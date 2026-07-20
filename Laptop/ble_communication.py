#!/usr/bin/env python3
"""
FocusFlow Lite — 笔记本端 BLE 双向通信模块 (ble_communication.py)
==============================================================

实现笔记本 (GATT Client) ↔ UNO Q Linux (GATT Server) 的完整双向蓝牙通信。

功能:
  1. BLE 设备扫描 & 自动连接 (过滤设备名 "UNO-Q-FF01")
  2. GATT Client: 发现服务、订阅 Notify、写入特征值
  3. 数据包序列化/反序列化 (JSON + 可选二进制)
  4. 心跳保活 (1Hz, 3s 超时)
  5. 断线自动重连 (指数退避)
  6. 系统状态同步 (双向)
  7. BLESimulator 模拟器 (无需实体 UNO Q, 用于 PC 端独立开发调试)
  8. FocusFlowBridge — 一键桥接 EyeTracker + ScreenMonitor → BLE

协议规范: 详见 ble_protocol.md
依赖: bleak (跨平台 BLE), PyQt5 (可选, 用于信号)

用法:
    # 方式 1: 真实 BLE 连接
    from ble_communication import BLEClient, SystemState
    client = BLEClient(device_name="UNO-Q-FF01")
    client.start()
    client.send_sensor_data(eye_features, screen_features)

    # 方式 2: 模拟器 (无 UNO Q 时开发用)
    from ble_communication import BLESimulator
    sim = BLESimulator()
    sim.start()
    sim.send_sensor_data(...)  # 模拟发送，打印日志

    # 方式 3: 一键桥接 (推荐)
    from ble_communication import FocusFlowBridge
    bridge = FocusFlowBridge(eye_tracker, screen_monitor)
    bridge.start()  # 自动 BLE 连接 + 1Hz 数据流

作者: D 同学 (FocusFlow 小组)
日期: 2026-07-20
协议版本: v1.0
"""

import time
import json
import struct
import logging
import threading
import queue
from typing import Optional, Callable, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logger = logging.getLogger("ble_communication")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(_handler)


# ═══════════════════════════════════════════════════════════════════════════════
# 协议常量
# ═══════════════════════════════════════════════════════════════════════════════

# BLE GATT UUIDs
SERVICE_UUID        = "0000FF00-0000-1000-8000-00805F9B34FB"
CHAR_LAPTOP_TX      = "0000FF01-0000-1000-8000-00805F9B34FB"  # Write: 笔记本 → UNO Q
CHAR_UNO_TX         = "0000FF02-0000-1000-8000-00805F9B34FB"  # Notify: UNO Q → 笔记本
CHAR_STATE_SYNC     = "0000FF03-0000-1000-8000-00805F9B34FB"  # Write+Notify: 双向状态
CHAR_HEARTBEAT      = "0000FF04-0000-1000-8000-00805F9B34FB"  # Write: 心跳

# 协议版本
PROTOCOL_VERSION = 1

# 默认配置
DEFAULT_DEVICE_NAME = "UNO-Q-FF01"
DEFAULT_MTU = 512
SENSOR_DATA_INTERVAL = 1.0       # 传感器数据发送频率 (秒)
HEARTBEAT_INTERVAL = 1.0          # 心跳间隔 (秒)
STATE_SYNC_INTERVAL = 5.0         # 状态定时同步间隔 (秒)
HEARTBEAT_TIMEOUT = 3.0           # 心跳超时 (秒)
MAX_CACHED_PACKETS = 30           # 断线缓存最大数据包数
RECONNECT_INITIAL_DELAY = 1.0     # 重连初始等待 (秒)
RECONNECT_MAX_DELAY = 30.0        # 重连最大等待 (秒)
RECONNECT_BACKOFF = 2.0           # 重连退避因子
RECONNECT_JITTER = 0.1            # 重连抖动比例


# ═══════════════════════════════════════════════════════════════════════════════
# 枚举定义
# ═══════════════════════════════════════════════════════════════════════════════

class SystemState(Enum):
    """系统运行状态"""
    OFFLINE         = "offline"
    INITIALIZING    = "initializing"
    CALIBRATING     = "calibrating"
    MONITORING      = "monitoring"
    RESTING         = "resting"
    PAUSED          = "paused"
    ERROR           = "error"
    SHUTTING_DOWN   = "shutting_down"


class SubState(Enum):
    """监控子状态"""
    FOCUSED     = "focused"
    NORMAL      = "normal"
    AT_RISK     = "at_risk"
    DISTRACTED  = "distracted"
    SHORT_REST  = "short_rest"
    LONG_REST   = "long_rest"
    MANUAL_REST = "manual_rest"


class PacketType(Enum):
    """数据包类型"""
    SENSOR_DATA         = "sensor_data"
    DISTRACTION_EVENT   = "distraction_event"
    EVENT_ACK           = "event_ack"
    FEEDBACK_CMD        = "feedback_cmd"
    STATE_SYNC          = "state_sync"
    STATE_ACK           = "state_ack"
    ERROR               = "error"
    HEARTBEAT           = "heartbeat"


class EventType(Enum):
    """事件类型"""
    SLACKING        = "slacking"
    DISTRACTED_EYE  = "distracted_eye"
    DISTRACTED_EEG  = "distracted_eeg"
    FACE_LOST       = "face_lost"
    MULTI_MODAL     = "multi_modal"
    RESUMED_FOCUS   = "resumed_focus"
    REST_STARTED    = "rest_started"
    REST_ENDED      = "rest_ended"


# 错误码
ERROR_CODES = {
    "E001": "BLE 连接超时",
    "E002": "BLE 意外断开",
    "E003": "写入失败 (characteristic not found)",
    "E004": "UNO Q 处理超时 (ACK 超时)",
    "E005": "数据格式错误",
    "E006": "序列号异常",
    "E007": "MTU 不足",
    "E008": "UNO Q 内部错误",
    "E009": "脑电头环断连",
    "E010": "手环断连",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 配置类
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BLEConfig:
    """BLE 通信配置"""
    device_name: str = DEFAULT_DEVICE_NAME
    service_uuid: str = SERVICE_UUID
    char_laptop_tx: str = CHAR_LAPTOP_TX
    char_uno_tx: str = CHAR_UNO_TX
    char_state_sync: str = CHAR_STATE_SYNC
    char_heartbeat: str = CHAR_HEARTBEAT
    mtu: int = DEFAULT_MTU
    sensor_interval: float = SENSOR_DATA_INTERVAL
    heartbeat_interval: float = HEARTBEAT_INTERVAL
    state_sync_interval: float = STATE_SYNC_INTERVAL
    heartbeat_timeout: float = HEARTBEAT_TIMEOUT
    max_cached_packets: int = MAX_CACHED_PACKETS
    reconnect_initial: float = RECONNECT_INITIAL_DELAY
    reconnect_max: float = RECONNECT_MAX_DELAY
    reconnect_backoff: float = RECONNECT_BACKOFF
    reconnect_jitter: float = RECONNECT_JITTER
    use_binary: bool = False  # 是否使用二进制压缩格式


# ═══════════════════════════════════════════════════════════════════════════════
# 数据包构建器
# ═══════════════════════════════════════════════════════════════════════════════

class PacketBuilder:
    """
    数据包构建 & 解析工具。

    负责:
      - 构建符合协议规范的 JSON 数据包
      - 序列号管理 (单调递增)
      - JSON ↔ 二进制 编码/解码
    """

    def __init__(self):
        self._seq = 0
        self._last_recv_seq: Dict[str, int] = {}  # 按 type 记录最后收到的 seq

    def next_seq(self) -> int:
        """获取下一个序列号 (单调递增, 溢出回绕)"""
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return seq

    def reset_seq(self):
        """重置序列号 (重连后调用)"""
        self._seq = 0
        self._last_recv_seq.clear()

    def build(self, packet_type: PacketType, payload: dict) -> dict:
        """
        构建完整数据包 (JSON 格式)。

        参数:
          packet_type: 数据包类型
          payload:     负载数据

        返回:
          完整的 JSON 包 (dict)
        """
        return {
            "ver": PROTOCOL_VERSION,
            "type": packet_type.value,
            "seq": self.next_seq(),
            "ts": time.time(),
            "payload": payload,
        }

    def build_sensor_data(self,
                          eye_features: Tuple[float, float, int, float, float],
                          screen_features: Tuple[float, float, int],
                          eye_state: str = "未知",
                          screen_state: str = "未知",
                          screen_app: str = "",
                          focus_score: float = 0.5,
                          alerts: list = None) -> dict:
        """
        构建 sensor_data 包。

        参数:
          eye_features:    (yaw, pitch, is_focused, state_duration, confidence)
          screen_features: (state_code, confidence, app_category)
        """
        yaw, pitch, is_focused, state_duration, confidence = eye_features
        state_code, scr_confidence, app_category = screen_features

        return self.build(PacketType.SENSOR_DATA, {
            "eye": {
                "yaw": round(yaw, 2),
                "pitch": round(pitch, 2),
                "roll": 0.0,  # 暂不从 eye_tracker 独立获取
                "is_focused": int(is_focused),
                "state_duration": round(state_duration, 2),
                "confidence": round(confidence, 3),
                "focus_score": round(focus_score, 3),
                "state": eye_state,
            },
            "screen": {
                "state_code": round(state_code, 2),
                "confidence": round(scr_confidence, 3),
                "app_category": int(app_category),
                "state": screen_state,
                "app": screen_app,
            },
            "combined": {
                "overall_focus": round(focus_score, 3),
                "alerts": alerts or [],
            },
        })

    def build_distraction_event(self,
                                event_type: EventType,
                                severity: str,
                                source: str,
                                details: dict,
                                eye_snapshot: dict = None,
                                screen_snapshot: dict = None) -> dict:
        """构建 distraction_event 包。"""
        now = time.time()
        event_id = f"{event_type.value}_{time.strftime('%Y%m%d_%H%M%S')}_{self.next_seq()}"

        return self.build(PacketType.DISTRACTION_EVENT, {
            "event_id": event_id,
            "event_type": event_type.value,
            "severity": severity,
            "source": source,
            "details": details,
            "eye_snapshot": eye_snapshot or {},
            "screen_snapshot": screen_snapshot or {},
        })

    def build_state_sync(self,
                         system_state: SystemState,
                         sub_state: SubState = None,
                         pomodoro: dict = None,
                         timer: dict = None,
                         errors: list = None,
                         warnings: list = None) -> dict:
        """构建 state_sync 包。"""
        return self.build(PacketType.STATE_SYNC, {
            "system_state": system_state.value,
            "sub_state": sub_state.value if sub_state else None,
            "pomodoro": pomodoro or {},
            "timer": timer or {},
            "errors": errors or [],
            "warnings": warnings or [],
        })

    def build_event_ack(self, event_id: str, success: bool = True) -> dict:
        """构建 event_ack 包。"""
        return self.build(PacketType.EVENT_ACK, {
            "event_id": event_id,
            "success": success,
        })

    def build_error(self, error_code: str, message: str,
                    severity: str = "warning", source: str = "laptop",
                    recoverable: bool = True) -> dict:
        """构建 error 包。"""
        return self.build(PacketType.ERROR, {
            "error_code": error_code,
            "message": message,
            "severity": severity,
            "source": source,
            "recoverable": recoverable,
        })

    def to_json(self, packet: dict) -> str:
        """将数据包序列化为 JSON 字符串。"""
        return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))

    def from_json(self, data: str) -> Optional[dict]:
        """
        从 JSON 字符串解析数据包。

        去重: 记录每个 type 的最后 seq，丢弃 ≤ 旧值的包。
        """
        try:
            packet = json.loads(data)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
            return None

        # 验证必填字段
        for field in ("ver", "type", "seq", "ts", "payload"):
            if field not in packet:
                logger.warning(f"数据包缺少字段: {field}")
                return None

        # 序列号去重
        ptype = packet["type"]
        seq = packet["seq"]
        if ptype in self._last_recv_seq:
            last = self._last_recv_seq[ptype]
            if seq <= last and last - seq < 0x80000000:
                # seq ≤ last 且不是回绕 (允许回绕)
                logger.debug(f"丢弃重复包: type={ptype}, seq={seq} <= last={last}")
                return None

        self._last_recv_seq[ptype] = seq
        return packet

    @staticmethod
    def pack_heartbeat(counter: int) -> bytes:
        """构建二进制心跳包: 1 byte counter (0-255)。"""
        return struct.pack("B", counter & 0xFF)

    # ---- 二进制格式 (可选优化) ----
    @staticmethod
    def pack_sensor_data_binary(eye_features, screen_features) -> bytes:
        """
        传感器数据 → 29 bytes 二进制格式。
        详见 ble_protocol.md §7.1。
        """
        yaw, pitch, is_focused, state_duration, confidence = eye_features
        state_code, scr_confidence, app_category = screen_features

        return struct.pack(
            ">BBIIIhhhBHBBBBbB",
            2,                              # version (uint8)
            0x01,                           # packet_type = sensor_data (uint8)
            0,                              # seq (placeholder, uint32)
            int(time.time()),               # ts_sec (uint32)
            0,                              # ts_usec (placeholder, uint32)
            int(yaw * 100),                 # eye_yaw × 100 (int16)
            int(pitch * 100),               # eye_pitch × 100 (int16)
            0,                              # eye_roll × 100 (int16)
            int(is_focused),                # eye_is_focused (uint8: 0/1)
            int(state_duration * 10),       # eye_state_duration × 10 (uint16)
            int(min(confidence, 1.0) * 200),# eye_confidence 0-200 (uint8)
            0,                              # eye_focus_score (placeholder, uint8)
            int(state_code * 200),          # screen_state_code (uint8)
            int(min(scr_confidence, 1.0) * 200),  # screen_confidence (uint8)
            app_category,                   # screen_app_category (int8)
            0,                              # combined_focus (placeholder, uint8)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# BLE 客户端 (笔记本端 GATT Client)
# ═══════════════════════════════════════════════════════════════════════════════

class BLEClient:
    """
    笔记本端 BLE GATT 客户端。

    职责:
      - 扫描并连接 UNO Q (过滤设备名)
      - 维护 BLE 连接 (心跳、重连)
      - 发送传感器数据 (1Hz)
      - 发送事件通知 (即时)
      - 接收 UNO Q 反馈指令
      - 系统状态同步

    回调:
      on_feedback_cmd(cmd: dict)       — 收到 UNO Q 反馈指令
      on_state_change(old, new)        — 系统状态变化
      on_connection_change(connected)  — 连接状态变化
      on_error(error: dict)            — 错误通知
      on_sensor_acked(seq: int)        — 传感器数据已被处理 (可选)
    """

    def __init__(self,
                 device_name: str = DEFAULT_DEVICE_NAME,
                 config: BLEConfig = None,
                 eye_tracker=None,
                 screen_monitor=None):
        """
        初始化 BLE 客户端。

        参数:
          device_name:    扫描过滤的设备名
          config:         BLE 配置 (可选)
          eye_tracker:    EyeTracker 实例 (用于自动提取特征, 可选)
          screen_monitor: ScreenMonitor 实例 (用于自动提取特征, 可选)
        """
        self.device_name = device_name
        self.config = config or BLEConfig(device_name=device_name)
        self._eye_tracker = eye_tracker
        self._screen_monitor = screen_monitor

        # ---- 运行时状态 (线程安全) ----
        self._lock = threading.Lock()
        self._running = False
        self._connected = False
        self._connection_lost = False

        # bleak 客户端引用
        self._client = None  # bleak.BleakClient
        self._device = None  # bleak.BleakScanner 发现的设备

        # 特征值句柄 (连接成功后填充)
        self._handle_laptop_tx = None
        self._handle_uno_tx = None
        self._handle_state_sync = None
        self._handle_heartbeat = None

        # ---- 数据包管理 ----
        self._packet_builder = PacketBuilder()
        self._heartbeat_counter = 0
        self._last_heartbeat_time = 0.0

        # 断线缓存
        self._cache_queue: deque = deque(maxlen=self.config.max_cached_packets)

        # 待确认事件
        self._pending_acks: Dict[str, float] = {}  # event_id → send_time

        # ---- 线程 ----
        self._main_thread: Optional[threading.Thread] = None
        self._send_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

        # ---- 系统状态 ----
        self._system_state = SystemState.OFFLINE
        self._sub_state: Optional[SubState] = None

        # ---- 统计 ----
        self._stats = {
            "packets_sent": 0,
            "packets_received": 0,
            "bytes_sent": 0,
            "bytes_received": 0,
            "reconnects": 0,
            "connection_drops": 0,
            "session_start": 0.0,
            "last_connect_time": 0.0,
            "last_disconnect_time": 0.0,
        }

        # ---- 回调 ----
        self.on_feedback_cmd: Optional[Callable[[dict], None]] = None
        self.on_state_change: Optional[Callable[[SystemState, SystemState], None]] = None
        self.on_connection_change: Optional[Callable[[bool, dict], None]] = None
        self.on_error: Optional[Callable[[dict], None]] = None
        self.on_sensor_acked: Optional[Callable[[int], None]] = None

        logger.info(f"BLEClient 初始化: target={device_name}")

    # ------------------------------------------------------------------
    # 公开 API — 生命周期
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """
        启动 BLE 客户端: 扫描 → 连接 → 开始数据流。
        阻塞直到连接成功或超时。
        """
        if self._running:
            logger.warning("BLEClient 已在运行中")
            return self._connected

        self._running = True
        self._stats["session_start"] = time.time()

        # 启动主线程
        self._main_thread = threading.Thread(
            target=self._main_loop, name="BLEClient-Main", daemon=True
        )
        self._main_thread.start()

        logger.info("BLEClient 已启动")
        return True

    def stop(self) -> None:
        """停止 BLE 客户端，断开连接。"""
        logger.info("BLEClient 正在停止...")
        self._running = False

        # 发送离线状态
        if self._connected:
            try:
                self._write_state_sync(SystemState.SHUTTING_DOWN)
            except Exception:
                pass

        # 清理连接
        self._disconnect()

        # 等待线程退出
        for t in [self._main_thread, self._send_thread, self._heartbeat_thread]:
            if t and t.is_alive():
                t.join(timeout=2.0)

        self._system_state = SystemState.OFFLINE
        logger.info("BLEClient 已停止")

    # ------------------------------------------------------------------
    # 公开 API — 数据发送
    # ------------------------------------------------------------------

    def send_sensor_data(self,
                         eye_features: Tuple[float, float, int, float, float],
                         screen_features: Tuple[float, float, int],
                         eye_state: str = "未知",
                         screen_state: str = "未知",
                         screen_app: str = "",
                         focus_score: float = 0.5,
                         alerts: list = None) -> bool:
        """
        发送传感器数据包。

        参数:
          eye_features:    (yaw, pitch, is_focused, state_duration, confidence)
          screen_features: (state_code, confidence, app_category)
        返回:
          是否成功发送 (False 表示已缓存或失败)
        """
        packet = self._packet_builder.build_sensor_data(
            eye_features=eye_features,
            screen_features=screen_features,
            eye_state=eye_state,
            screen_state=screen_state,
            screen_app=screen_app,
            focus_score=focus_score,
            alerts=alerts,
        )
        return self._send_packet(packet)

    def send_distraction_event(self,
                               event_type: EventType,
                               severity: str,
                               source: str,
                               details: dict,
                               eye_snapshot: dict = None,
                               screen_snapshot: dict = None) -> Optional[str]:
        """
        发送走神事件 (即时发送，高优先级)。

        返回:
          事件 ID (用于追踪 ACK), 发送失败返回 None
        """
        packet = self._packet_builder.build_distraction_event(
            event_type=event_type,
            severity=severity,
            source=source,
            details=details,
            eye_snapshot=eye_snapshot,
            screen_snapshot=screen_snapshot,
        )
        event_id = packet["payload"]["event_id"]
        success = self._send_packet(packet, high_priority=True)
        if success:
            self._pending_acks[event_id] = time.time()
            return event_id
        return None

    def set_state(self, system_state: SystemState,
                  sub_state: SubState = None,
                  pomodoro: dict = None,
                  timer: dict = None) -> bool:
        """
        设置系统状态并同步到 UNO Q。

        返回:
          是否成功
        """
        old_state = self._system_state

        with self._lock:
            self._system_state = system_state
            self._sub_state = sub_state

        # 触发回调
        if old_state != system_state and self.on_state_change:
            try:
                self.on_state_change(old_state, system_state)
            except Exception as e:
                logger.error(f"状态回调异常: {e}")

        # 同步到 UNO Q
        packet = self._packet_builder.build_state_sync(
            system_state=system_state,
            sub_state=sub_state,
            pomodoro=pomodoro,
            timer=timer,
        )
        return self._send_packet(packet)

    # ------------------------------------------------------------------
    # 公开 API — 状态查询
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def system_state(self) -> SystemState:
        with self._lock:
            return self._system_state

    @property
    def sub_state(self) -> Optional[SubState]:
        with self._lock:
            return self._sub_state

    @property
    def stats(self) -> Dict[str, Any]:
        """获取连接统计。"""
        with self._lock:
            s = dict(self._stats)
        s["session_elapsed"] = (
            time.time() - s["session_start"] if s["session_start"] > 0 else 0
        )
        s["packets_cached"] = len(self._cache_queue)
        s["pending_acks"] = len(self._pending_acks)
        return s

    # ------------------------------------------------------------------
    # 内部: 主循环
    # ------------------------------------------------------------------

    def _main_loop(self) -> None:
        """主循环: 连接管理 + 重连。"""
        while self._running:
            try:
                # 1. 扫描并连接
                if not self._connected:
                    success = self._scan_and_connect()
                    if not success:
                        # 等待后重试
                        self._wait_reconnect()
                        continue

                # 2. 已连接 → 等待断开
                while self._connected and self._running:
                    time.sleep(0.5)

                    # 检查心跳超时 (仅当 UNO Q 也会发心跳时才需要)
                    # 笔记本端不检查 UNO Q 心跳，因为 UNO Q 不发心跳

            except Exception as e:
                logger.error(f"主循环异常: {e}", exc_info=True)
                self._disconnect()
                time.sleep(1.0)

    def _scan_and_connect(self) -> bool:
        """
        扫描并连接 UNO Q 设备。
        返回 True 表示连接成功。
        """
        try:
            import bleak
        except ImportError:
            logger.error("未安装 bleak 库。请执行: pip install bleak")
            self._emit_error("E003", "bleak 库未安装", recoverable=False)
            return False

        # 更新状态
        self._set_system_state(SystemState.INITIALIZING)

        logger.info(f"正在扫描 BLE 设备 (过滤: {self.device_name})...")

        try:
            # 扫描设备
            scanner = bleak.BleakScanner()
            devices = scanner.discovered_devices

            # 查找目标设备
            target = None
            for d in devices:
                if d.name and self.device_name.lower() in d.name.lower():
                    target = d
                    break

            if target is None:
                # 主动扫描
                logger.info("未在缓存中找到设备，开始主动扫描...")
                # 简化: 使用同步扫描 (bleak 0.21+)
                try:
                    from bleak import BleakScanner
                    import asyncio

                    async def _scan():
                        return await BleakScanner.find_device_by_filter(
                            lambda d, ad: d.name and self.device_name.lower() in d.name.lower(),
                            timeout=5.0,
                        )

                    # 在新的事件循环中运行
                    loop = asyncio.new_event_loop()
                    target = loop.run_until_complete(_scan())
                    loop.close()
                except Exception:
                    pass

            if target is None:
                logger.warning(f"未发现设备: {self.device_name}")
                return False

            logger.info(f"发现设备: {target.name} ({target.address})")
            self._device = target

            # 连接
            logger.info(f"正在连接 {target.address}...")

            async def _connect():
                client = bleak.BleakClient(
                    target.address,
                    timeout=10.0,
                )
                await client.connect()
                return client

            loop = asyncio.new_event_loop()
            self._client = loop.run_until_complete(_connect())
            loop.close()

            if not self._client.is_connected:
                logger.error("连接失败")
                return False

            # 发现服务 & 特征值
            loop = asyncio.new_event_loop()
            success = loop.run_until_complete(self._discover_characteristics())
            loop.close()

            if not success:
                logger.error("服务发现失败")
                async def _disconnect():
                    await self._client.disconnect()
                loop = asyncio.new_event_loop()
                loop.run_until_complete(_disconnect())
                loop.close()
                self._client = None
                return False

            # 订阅 UNO_TX Notify
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._subscribe_uno_tx())
            loop.close()

            # 连接成功
            with self._lock:
                self._connected = True
                self._connection_lost = False
                self._stats["last_connect_time"] = time.time()

            logger.info(f"✅ BLE 连接成功: {self.device_name} ({target.address})")

            # 同步初始状态
            self._write_state_sync(SystemState.MONITORING)

            # 重置序列号
            self._packet_builder.reset_seq()

            # 启动心跳 & 发送线程
            self._start_workers()

            # 重发缓存的数据
            self._flush_cache()

            # 触发回调
            if self.on_connection_change:
                try:
                    self.on_connection_change(True, {
                        "device_name": target.name,
                        "address": target.address,
                    })
                except Exception as e:
                    logger.error(f"连接回调异常: {e}")

            return True

        except Exception as e:
            logger.error(f"连接失败: {e}", exc_info=True)
            self._disconnect()
            return False

    async def _discover_characteristics(self) -> bool:
        """发现 GATT 服务和特征值。"""
        if not self._client:
            return False

        try:
            # 遍历服务
            for service in self._client.services:
                if service.uuid.lower() == self.config.service_uuid.lower():
                    for char in service.characteristics:
                        uuid_lower = char.uuid.lower()
                        if uuid_lower == self.config.char_laptop_tx.lower():
                            self._handle_laptop_tx = char
                        elif uuid_lower == self.config.char_uno_tx.lower():
                            self._handle_uno_tx = char
                        elif uuid_lower == self.config.char_state_sync.lower():
                            self._handle_state_sync = char
                        elif uuid_lower == self.config.char_heartbeat.lower():
                            self._handle_heartbeat = char
                    break

            # 检查所有特征值是否找到
            found_all = all([
                self._handle_laptop_tx,
                self._handle_uno_tx,
                self._handle_state_sync,
                self._handle_heartbeat,
            ])

            if found_all:
                logger.info("✅ 所有 GATT 特征值已发现")
            else:
                missing = []
                if not self._handle_laptop_tx: missing.append("LAPTOP_TX")
                if not self._handle_uno_tx: missing.append("UNO_TX")
                if not self._handle_state_sync: missing.append("STATE_SYNC")
                if not self._handle_heartbeat: missing.append("HEARTBEAT")
                logger.warning(f"部分特征值未找到: {missing}")

            return found_all

        except Exception as e:
            logger.error(f"服务发现异常: {e}")
            return False

    async def _subscribe_uno_tx(self) -> None:
        """订阅 UNO_TX Notify。"""
        if self._client and self._handle_uno_tx:
            await self._client.start_notify(
                self._handle_uno_tx.uuid,
                self._on_uno_notify,
            )
            logger.info("已订阅 UNO_TX Notify")

    def _on_uno_notify(self, sender, data: bytearray) -> None:
        """
        收到 UNO Q 的 Notify 数据。
        在 bleak 的回调线程中执行，需要线程安全。
        """
        try:
            text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
            packet = self._packet_builder.from_json(text)
            if packet is None:
                return

            with self._lock:
                self._stats["packets_received"] += 1
                self._stats["bytes_received"] += len(data)

            self._handle_packet(packet)

        except Exception as e:
            logger.error(f"处理 Notify 异常: {e}", exc_info=True)

    def _on_ble_disconnect(self, client) -> None:
        """BLE 断开回调。"""
        logger.warning("BLE 连接已断开")
        self._disconnect()

    # ------------------------------------------------------------------
    # 内部: 数据包处理
    # ------------------------------------------------------------------

    def _handle_packet(self, packet: dict) -> None:
        """处理收到的数据包。"""
        ptype = packet["type"]
        payload = packet.get("payload", {})

        if ptype == PacketType.FEEDBACK_CMD.value:
            # 收到反馈指令
            logger.info(f"收到反馈指令: {payload.get('cmd_type')} -> {payload.get('target')}")
            if self.on_feedback_cmd:
                try:
                    self.on_feedback_cmd(payload)
                except Exception as e:
                    logger.error(f"反馈回调异常: {e}")

        elif ptype == PacketType.STATE_SYNC.value:
            # UNO Q 状态同步
            unq_state = payload.get("system_state", "unknown")
            logger.debug(f"UNO Q 状态同步: {unq_state}")

        elif ptype == PacketType.STATE_ACK.value:
            # 状态确认
            logger.debug("收到状态确认")

        elif ptype == PacketType.ERROR.value:
            # 错误通知
            logger.warning(f"UNO Q 错误: {payload.get('error_code')} - {payload.get('message')}")
            if self.on_error:
                try:
                    self.on_error(payload)
                except Exception as e:
                    logger.error(f"错误回调异常: {e}")

    # ------------------------------------------------------------------
    # 内部: 发送
    # ------------------------------------------------------------------

    def _send_packet(self, packet: dict, high_priority: bool = False) -> bool:
        """
        发送数据包到 UNO Q。

        参数:
          packet:        数据包
          high_priority: 是否插队发送 (事件包)
        返回:
          是否成功发送
        """
        json_str = self._packet_builder.to_json(packet)
        data = json_str.encode("utf-8")

        # 检查连接状态
        if not self._connected or not self._client:
            # 缓存
            if len(self._cache_queue) < self.config.max_cached_packets:
                self._cache_queue.append(data)
                logger.debug(f"数据包已缓存 ({len(self._cache_queue)}/{self.config.max_cached_packets})")
            return False

        # 检查 MTU
        if len(data) > self.config.mtu:
            logger.warning(f"数据包 {len(data)}B 超过 MTU {self.config.mtu}B, 将被截断")

        try:
            # 发送到 LAPTOP_TX
            uuid = self._handle_laptop_tx.uuid if self._handle_laptop_tx else self.config.char_laptop_tx

            # 使用 bleak 异步发送
            import asyncio

            async def _write():
                await self._client.write_gatt_char(uuid, data, response=False)

            loop = asyncio.new_event_loop()
            loop.run_until_complete(_write())
            loop.close()

            with self._lock:
                self._stats["packets_sent"] += 1
                self._stats["bytes_sent"] += len(data)

            logger.debug(f"发送 {packet['type']} (seq={packet['seq']}, {len(data)}B)")
            return True

        except Exception as e:
            logger.error(f"发送失败: {e}")
            # 缓存
            if len(self._cache_queue) < self.config.max_cached_packets:
                self._cache_queue.append(data)
            return False

    def _write_state_sync(self, state: SystemState, sub_state: SubState = None) -> bool:
        """写入状态同步 (STATE_SYNC 特征值)。"""
        packet = self._packet_builder.build_state_sync(
            system_state=state,
            sub_state=sub_state,
        )
        json_str = self._packet_builder.to_json(packet)
        data = json_str.encode("utf-8")

        if not self._connected or not self._client:
            return False

        try:
            uuid = self._handle_state_sync.uuid if self._handle_state_sync else self.config.char_state_sync

            import asyncio
            async def _write():
                await self._client.write_gatt_char(uuid, data, response=False)
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_write())
            loop.close()
            return True
        except Exception as e:
            logger.error(f"状态写入失败: {e}")
            return False

    def _send_heartbeat(self) -> None:
        """发送心跳包 (1 byte 二进制)。"""
        if not self._connected or not self._client:
            return

        try:
            self._heartbeat_counter = (self._heartbeat_counter + 1) & 0xFF
            data = PacketBuilder.pack_heartbeat(self._heartbeat_counter)

            uuid = self._handle_heartbeat.uuid if self._handle_heartbeat else self.config.char_heartbeat

            import asyncio
            async def _write():
                await self._client.write_gatt_char(uuid, data, response=False)
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_write())
            loop.close()

            self._last_heartbeat_time = time.time()

        except Exception as e:
            logger.error(f"心跳发送失败: {e}")
            self._connection_lost = True

    # ------------------------------------------------------------------
    # 内部: 线程管理
    # ------------------------------------------------------------------

    def _start_workers(self) -> None:
        """启动心跳和发送工作线程。"""
        # 心跳线程
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_worker, name="BLEClient-HB", daemon=True
            )
            self._heartbeat_thread.start()

        # 传感器数据发送线程
        if self._send_thread is None or not self._send_thread.is_alive():
            self._send_thread = threading.Thread(
                target=self._send_worker, name="BLEClient-Send", daemon=True
            )
            self._send_thread.start()

    def _heartbeat_worker(self) -> None:
        """心跳发送线程 (1Hz)。"""
        while self._running and self._connected:
            self._send_heartbeat()

            if self._connection_lost:
                logger.warning("心跳持续失败，触发重连")
                self._disconnect()
                break

            time.sleep(self.config.heartbeat_interval)

    def _send_worker(self) -> None:
        """传感器数据发送线程 (1Hz): 从外部模块拉取最新数据并发送。"""
        while self._running and self._connected:
            if self._eye_tracker and self._screen_monitor:
                try:
                    # 从外部模块提取特征
                    self._send_current_features()
                except Exception as e:
                    logger.error(f"传感器发送异常: {e}")

            time.sleep(self.config.sensor_interval)

    def _send_current_features(self) -> None:
        """从关联的 EyeTracker/ScreenMonitor 提取当前特征并发送。"""
        if not self._eye_tracker or not self._screen_monitor:
            return

        gaze = self._eye_tracker.get_state()
        screen = self._screen_monitor.get_last_state()

        self.send_sensor_data(
            eye_features=gaze.feature_vector,
            screen_features=screen.feature_vector,
            eye_state=gaze.state.value,
            screen_state=screen.state.value,
            screen_app=screen.app,
            focus_score=gaze.focus_score,
        )

    # ------------------------------------------------------------------
    # 内部: 连接管理
    # ------------------------------------------------------------------

    def _disconnect(self) -> None:
        """清理 BLE 连接。"""
        was_connected = self._connected

        with self._lock:
            self._connected = False
            self._connection_lost = True

        if self._client:
            try:
                import asyncio
                async def _disconnect():
                    if self._client and self._client.is_connected:
                        await self._client.disconnect()
                loop = asyncio.new_event_loop()
                loop.run_until_complete(_disconnect())
                loop.close()
            except Exception as e:
                logger.debug(f"断开异常 (可忽略): {e}")
            self._client = None

        self._handle_laptop_tx = None
        self._handle_uno_tx = None
        self._handle_state_sync = None
        self._handle_heartbeat = None

        if was_connected:
            with self._lock:
                self._stats["connection_drops"] += 1
                self._stats["last_disconnect_time"] = time.time()

            if self.on_connection_change:
                try:
                    self.on_connection_change(False, {"reason": "disconnected"})
                except Exception:
                    pass

            logger.warning("BLE 连接已清理")

    def _wait_reconnect(self) -> None:
        """重连等待 (指数退避)。"""
        delay = min(
            self.config.reconnect_initial *
            (self.config.reconnect_backoff ** self._stats["reconnects"]),
            self.config.reconnect_max,
        )
        # 添加随机抖动
        import random
        jitter = delay * self.config.reconnect_jitter * (random.random() * 2 - 1)
        delay += jitter

        logger.info(f"重连等待 {delay:.1f}s (尝试 #{self._stats['reconnects'] + 1})")

        with self._lock:
            self._stats["reconnects"] += 1

        # 分段 sleep 以便快速响应 stop()
        remaining = delay
        while remaining > 0 and self._running:
            time.sleep(min(0.5, remaining))
            remaining -= 0.5

    def _flush_cache(self) -> None:
        """重连后重新发送缓存的数据包。"""
        if not self._cache_queue:
            return

        logger.info(f"重发缓存数据 ({len(self._cache_queue)} 条)...")
        count = 0
        while self._cache_queue and self._connected:
            data = self._cache_queue.popleft()
            try:
                uuid = self._handle_laptop_tx.uuid if self._handle_laptop_tx else self.config.char_laptop_tx
                import asyncio
                async def _write():
                    await self._client.write_gatt_char(uuid, data, response=False)
                loop = asyncio.new_event_loop()
                loop.run_until_complete(_write())
                loop.close()
                count += 1
            except Exception as e:
                logger.error(f"缓存重发失败: {e}")
                break

        logger.info(f"缓存重发完成: {count}/{count + len(self._cache_queue)} 条")

    # ------------------------------------------------------------------
    # 内部: 工具方法
    # ------------------------------------------------------------------

    def _set_system_state(self, state: SystemState) -> None:
        """内部设置系统状态 (触发回调)。"""
        old = self._system_state
        with self._lock:
            self._system_state = state
        if old != state and self.on_state_change:
            try:
                self.on_state_change(old, state)
            except Exception:
                pass

    def _emit_error(self, code: str, message: str,
                    severity: str = "warning", recoverable: bool = True) -> None:
        """发送错误通知。"""
        packet = self._packet_builder.build_error(
            error_code=code, message=message,
            severity=severity, recoverable=recoverable,
        )
        if self.on_error:
            try:
                self.on_error(packet["payload"])
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# BLE 模拟器 (无需实体 UNO Q, 开发调试用)
# ═══════════════════════════════════════════════════════════════════════════════

class BLESimulator:
    """
    BLE 通信模拟器。

    在不连接真实 UNO Q 的情况下模拟 BLE 通信:
      - 打印发送的数据包 (格式化 JSON)
      - 模拟 UNO Q 的反馈响应
      - 支持回调注册 (与 BLEClient 相同接口)

    用法:
        sim = BLESimulator()
        sim.start()
        sim.send_sensor_data(eye_features, screen_features, ...)
    """

    def __init__(self, verbose: bool = True, simulate_responses: bool = True):
        self.verbose = verbose
        self.simulate_responses = simulate_responses
        self._running = False
        self._packet_builder = PacketBuilder()
        self._system_state = SystemState.OFFLINE
        self._connected = False

        # 统计
        self._packets_sent = 0
        self._packets_received = 0

        # 回调 (与 BLEClient 相同接口)
        self.on_feedback_cmd: Optional[Callable[[dict], None]] = None
        self.on_state_change: Optional[Callable[[SystemState, SystemState], None]] = None
        self.on_connection_change: Optional[Callable[[bool, dict], None]] = None
        self.on_error: Optional[Callable[[dict], None]] = None

    def start(self) -> bool:
        self._running = True
        self._connected = True
        self._set_state(SystemState.MONITORING)

        if self.verbose:
            print("=" * 60)
            print("  [BLE Simulator] 模拟 BLE 连接已建立")
            print(f"  设备: {DEFAULT_DEVICE_NAME} (模拟)")
            print(f"  MTU:  {DEFAULT_MTU} bytes")
            print("=" * 60)

        if self.on_connection_change:
            try:
                self.on_connection_change(True, {
                    "device_name": DEFAULT_DEVICE_NAME,
                    "address": "00:00:00:00:00:00 (模拟)",
                })
            except Exception:
                pass

        return True

    def stop(self) -> None:
        self._running = False
        self._connected = False
        self._set_state(SystemState.OFFLINE)

        if self.verbose:
            print(f"\n[BLE Simulator] 已断开 | 发送: {self._packets_sent} 包")

    def send_sensor_data(self,
                         eye_features: Tuple[float, float, int, float, float],
                         screen_features: Tuple[float, float, int],
                         eye_state: str = "未知",
                         screen_state: str = "未知",
                         screen_app: str = "",
                         focus_score: float = 0.5,
                         alerts: list = None) -> bool:
        """模拟发送传感器数据。"""
        packet = self._packet_builder.build_sensor_data(
            eye_features=eye_features,
            screen_features=screen_features,
            eye_state=eye_state,
            screen_state=screen_state,
            screen_app=screen_app,
            focus_score=focus_score,
            alerts=alerts,
        )
        self._packets_sent += 1
        self._print_packet(packet, "SENSOR_DATA")
        return True

    def send_distraction_event(self,
                               event_type: EventType,
                               severity: str,
                               source: str,
                               details: dict,
                               eye_snapshot: dict = None,
                               screen_snapshot: dict = None) -> str:
        """模拟发送走神事件。"""
        packet = self._packet_builder.build_distraction_event(
            event_type=event_type,
            severity=severity,
            source=source,
            details=details,
            eye_snapshot=eye_snapshot,
            screen_snapshot=screen_snapshot,
        )
        event_id = packet["payload"]["event_id"]
        self._packets_sent += 1
        self._print_packet(packet, f"DISTRACTION_EVENT [{severity.upper()}]")

        # 模拟 UNO Q 响应
        if self.simulate_responses:
            self._simulate_response(event_type, severity, event_id)

        return event_id

    def set_state(self, system_state: SystemState,
                  sub_state: SubState = None) -> bool:
        """模拟状态同步。"""
        old = self._system_state
        self._system_state = system_state

        if old != system_state and self.on_state_change:
            try:
                self.on_state_change(old, system_state)
            except Exception:
                pass

        if self.verbose:
            print(f"  [BLE Sim] 状态: {old.value} → {system_state.value}")

        return True

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def system_state(self) -> SystemState:
        return self._system_state

    def _set_state(self, state: SystemState) -> None:
        old = self._system_state
        self._system_state = state
        if old != state and self.on_state_change:
            try:
                self.on_state_change(old, state)
            except Exception:
                pass

    def _simulate_response(self, event_type: EventType, severity: str, event_id: str) -> None:
        """模拟 UNO Q 的反馈响应。"""
        response_delay = 0.05  # 50ms

        if severity == "high":
            cmd = {
                "cmd_id": f"sim_{int(time.time())}",
                "cmd_type": "vibrate_and_display",
                "target": "wristband+oled",
                "params": {"pattern": "double_pulse", "count": 2},
                "display": {
                    "line1": "⚠️ 模 拟 告 警",
                    "line2": f"事件: {event_type.value}",
                    "line3": "来源: 模拟器",
                    "line4": "请回到工作",
                },
            }
        elif severity == "medium":
            cmd = {
                "cmd_id": f"sim_{int(time.time())}",
                "cmd_type": "vibrate",
                "target": "wristband",
                "params": {"pattern": "short", "count": 1},
            }
        else:
            cmd = {
                "cmd_id": f"sim_{int(time.time())}",
                "cmd_type": "oled_update",
                "target": "oled",
                "display": {"line1": f"状态已更新: {event_type.value}"},
            }

        if self.verbose:
            print(f"  ← [模拟 UNO Q 响应] {cmd['cmd_type']} ({response_delay*1000:.0f}ms)")

        if self.on_feedback_cmd:
            try:
                self.on_feedback_cmd(cmd)
            except Exception:
                pass

    def _print_packet(self, packet: dict, label: str) -> None:
        """格式化打印数据包。"""
        if not self.verbose:
            return

        print(f"\n  ── {label} (seq={packet['seq']}) ──")
        # 只打印关键的 payload 字段
        p = packet.get("payload", {})

        if "eye" in p:
            e = p["eye"]
            print(f"  👁 眼动: yaw={e['yaw']:+.1f}° pitch={e['pitch']:+.1f}° "
                  f"focused={e['is_focused']} dur={e['state_duration']:.1f}s "
                  f"conf={e['confidence']:.2f}")

        if "screen" in p:
            s = p["screen"]
            print(f"  📺 屏幕: state={s['state']} code={s['state_code']:.2f} "
                  f"app={s.get('app', 'N/A')} conf={s['confidence']:.2f}")

        if "combined" in p:
            c = p["combined"]
            print(f"  📊 综合: focus={c['overall_focus']:.2f} "
                  f"alerts={len(c.get('alerts', []))}")

        if "event_type" in p:
            print(f"  ⚡ 事件: {p['event_type']} severity={p.get('severity', '?')} "
                  f"source={p.get('source', '?')}")
            if "details" in p:
                print(f"      详情: {p['details']}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# FocusFlowBridge — 一键桥接
# ═══════════════════════════════════════════════════════════════════════════════

class FocusFlowBridge:
    """
    一键桥接: EyeTracker + ScreenMonitor → BLE 通信。

    自动完成:
      - 每秒从 EyeTracker/ScreenMonitor 拉取特征
      - 检测走神事件 → 即时发送 distraction_event
      - 管理系统状态 (monitoring/resting/paused)
      - 连接生命周期管理

    用法:
        from eye_tracker import EyeTracker
        from screen_monitor import ScreenMonitor
        from ble_communication import FocusFlowBridge

        eye = EyeTracker(); eye.start()
        scr = ScreenMonitor(api_key="..."); scr.start()

        bridge = FocusFlowBridge(eye, scr, use_simulator=True)
        bridge.start()

        # ... 运行中 ...

        bridge.stop()
    """

    def __init__(self,
                 eye_tracker,
                 screen_monitor,
                 use_simulator: bool = False,
                 ble_config: BLEConfig = None):
        """
        参数:
          eye_tracker:    EyeTracker 实例
          screen_monitor: ScreenMonitor 实例
          use_simulator:  是否使用模拟器 (True=无需 UNO Q, False=真实 BLE)
          ble_config:     BLE 配置
        """
        self._eye = eye_tracker
        self._screen = screen_monitor
        self._use_simulator = use_simulator
        self._config = ble_config or BLEConfig()

        # 创建通信后端
        if use_simulator:
            self._ble = BLESimulator(verbose=True, simulate_responses=True)
        else:
            self._ble = BLEClient(
                device_name=self._config.device_name,
                config=self._config,
                eye_tracker=eye_tracker,
                screen_monitor=screen_monitor,
            )

        # 注册回调
        self._ble.on_feedback_cmd = self._on_feedback
        self._ble.on_connection_change = self._on_connection

        # ---- 走神追踪 ----
        self._last_gaze_state = "未知"
        self._distraction_start_time: float = 0.0
        self._distraction_reported: bool = False
        self._resting = False

        # ---- 线程 ----
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

        logger.info(f"FocusFlowBridge 初始化: backend={'simulator' if use_simulator else 'BLE'}")

    def start(self) -> bool:
        """启动桥接: 连接 BLE, 开始监控循环。"""
        if self._running:
            logger.warning("FocusFlowBridge 已在运行中")
            return True

        # 启动 BLE 后端
        ok = self._ble.start()
        if not ok:
            logger.error("BLE 后端启动失败")
            return False

        # 初始化状态
        self._ble.set_state(SystemState.MONITORING)

        # 启动监控线程
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="FocusFlowBridge", daemon=True
        )
        self._monitor_thread.start()

        logger.info("FocusFlowBridge 已启动")
        return True

    def stop(self) -> None:
        """停止桥接。"""
        self._running = False

        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)

        self._ble.stop()
        logger.info("FocusFlowBridge 已停止")

    def set_resting(self, resting: bool, duration_min: int = 5) -> None:
        """切换休息状态。"""
        self._resting = resting
        if resting:
            self._ble.set_state(SystemState.RESTING, SubState.MANUAL_REST)
            self._send_rest_event(EventType.REST_STARTED, duration_min)
        else:
            self._ble.set_state(SystemState.MONITORING)
            self._send_rest_event(EventType.REST_ENDED, 0)

    # ------------------------------------------------------------------
    # 内部: 监控循环 (1Hz)
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """主监控循环: 每秒发送传感器数据 + 检测走神事件。"""
        while self._running:
            try:
                # 1. 获取当前状态
                gaze = self._eye.get_state()
                screen = self._screen.get_last_state()

                # 2. 发送传感器数据
                self._ble.send_sensor_data(
                    eye_features=gaze.feature_vector,
                    screen_features=screen.feature_vector,
                    eye_state=gaze.state.value,
                    screen_state=screen.state.value,
                    screen_app=screen.app,
                    focus_score=gaze.focus_score,
                )

                # 3. 检测走神事件
                if not self._resting:
                    self._check_distraction(gaze, screen)

            except Exception as e:
                logger.error(f"监控循环异常: {e}", exc_info=True)

            time.sleep(1.0)

    def _check_distraction(self, gaze, screen) -> None:
        """
        检测走神事件并发送。

        走神判定:
          - 摸鱼: 屏幕检测到摸鱼 → 立即发送 (高优先级)
          - 走神: 头部姿态持续走神 > 10s → 发送
          - 离开: 人脸丢失 > 5s → 发送
          - 恢复: 从走神恢复到专注 → 发送恢复事件
        """
        from eye_tracker import GazeState
        from screen_monitor import ScreenState

        now = time.time()

        # ---- 摸鱼检测 (屏幕) ----
        if screen.state == ScreenState.SLACKING and not self._distraction_reported:
            self._ble.send_distraction_event(
                event_type=EventType.SLACKING,
                severity="high",
                source="screen",
                details={
                    "app": screen.app,
                    "reason": screen.reason,
                    "duration_sec": 0,
                },
                screen_snapshot={
                    "state_code": screen.feature_vector[0],
                    "app_category": screen.feature_vector[2],
                    "confidence": screen.confidence,
                },
            )
            self._distraction_reported = True
            self._distraction_start_time = now
            return

        # ---- 摸鱼恢复 ----
        if self._distraction_reported and screen.state != ScreenState.SLACKING \
                and gaze.state == GazeState.FOCUSED:
            self._ble.send_distraction_event(
                event_type=EventType.RESUMED_FOCUS,
                severity="low",
                source="fusion",
                details={"duration_sec": now - self._distraction_start_time},
            )
            self._distraction_reported = False
            self._distraction_start_time = 0.0

        # ---- 持续走神 (眼动) ----
        if (gaze.state == GazeState.DISTRACTED and
                gaze.state_duration > 10 and
                not self._distraction_reported):
            self._ble.send_distraction_event(
                event_type=EventType.DISTRACTED_EYE,
                severity="medium",
                source="eye",
                details={"duration_sec": gaze.state_duration},
                eye_snapshot={
                    "yaw": gaze.head_pose.yaw,
                    "pitch": gaze.head_pose.pitch,
                    "is_focused": 0,
                },
            )
            self._distraction_reported = True
            self._distraction_start_time = now

        # ---- 人脸丢失 ----
        if (not gaze.face_detected and
                self._eye.is_camera_active and
                gaze.state_duration > 5 and
                not self._distraction_reported):
            self._ble.send_distraction_event(
                event_type=EventType.FACE_LOST,
                severity="medium",
                source="eye",
                details={"duration_sec": gaze.state_duration},
            )
            self._distraction_reported = True
            self._distraction_start_time = now

    def _send_rest_event(self, event_type: EventType, duration_min: int) -> None:
        """发送休息相关事件。"""
        details = {}
        if event_type == EventType.REST_STARTED:
            details["rest_duration_min"] = duration_min
        elif event_type == EventType.REST_ENDED:
            details["message"] = "休息结束，继续加油"

        self._ble.send_distraction_event(
            event_type=event_type,
            severity="low",
            source="system",
            details=details,
        )

    # ------------------------------------------------------------------
    # 内部: 回调
    # ------------------------------------------------------------------

    def _on_feedback(self, cmd: dict) -> None:
        """收到 UNO Q 反馈指令。"""
        cmd_type = cmd.get("cmd_type", "unknown")
        target = cmd.get("target", "unknown")
        logger.info(f"[Bridge] 收到反馈: {cmd_type} → {target}")
        # 如果需要，可以触发笔记本弹窗、声音等
        # 由上层 GUI 通过 on_feedback_cmd 属性自行处理

    def _on_connection(self, connected: bool, info: dict = None) -> None:
        """连接状态变化。"""
        if connected:
            logger.info(f"[Bridge] BLE 已连接: {info}")
        else:
            logger.warning(f"[Bridge] BLE 已断开: {info}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 命令行测试入口
# ═══════════════════════════════════════════════════════════════════════════════

def demo_simulator():
    """
    模拟器演示: 展示 BLE 协议通信流程。
    不需要摄像头、屏幕监控、UNO Q 硬件。
    """
    import random
    import signal

    print("=" * 60)
    print("  FocusFlow Lite — BLE 协议模拟演示")
    print("=" * 60)
    print()
    print("  此演示展示笔记本 ↔ UNO Q 之间的完整通信协议。")
    print("  无需任何硬件，所有数据均为模拟。")
    print()

    sim = BLESimulator(verbose=True, simulate_responses=True)
    sim.start()

    shutdown = threading.Event()
    signal.signal(signal.SIGINT, lambda s, f: shutdown.set())

    print("\n  开始模拟数据流 (Ctrl+C 退出)...\n")

    tick = 0
    try:
        while not shutdown.is_set():
            tick += 1

            # 模拟传感器数据
            yaw = random.uniform(-15, 15)
            pitch = random.uniform(-10, 10)
            is_focused = 1 if abs(yaw) < 20 and abs(pitch) < 15 else 0
            state_duration = random.uniform(0, 30)
            confidence = random.uniform(0.7, 0.95)

            eye_features = (yaw, pitch, is_focused, state_duration, confidence)

            state_code = random.choice([0.0, 0.3, 0.6, 1.0])
            scr_confidence = random.uniform(0.6, 0.95)
            app_category = int(state_code * 3)
            screen_features = (state_code, scr_confidence, app_category)

            screen_state_map = {0.0: "离开", 0.3: "摸鱼", 0.6: "一般浏览", 1.0: "专注工作"}
            eye_state = "专注" if is_focused else "走神"

            sim.send_sensor_data(
                eye_features=eye_features,
                screen_features=screen_features,
                eye_state=eye_state,
                screen_state=screen_state_map.get(state_code, "未知"),
                screen_app=random.choice(["VSCode", "PyCharm", "Chrome", "B站", "Word", "PDF"]),
                focus_score=random.uniform(0.5, 0.95),
            )

            # 每 15 秒模拟一次走神事件
            if tick % 15 == 0:
                event_type = random.choice([
                    EventType.SLACKING,
                    EventType.DISTRACTED_EYE,
                    EventType.FACE_LOST,
                ])
                severity = "high" if event_type == EventType.SLACKING else "medium"
                sim.send_distraction_event(
                    event_type=event_type,
                    severity=severity,
                    source=random.choice(["eye", "screen", "fusion"]),
                    details={
                        "app": "哔哩哔哩" if event_type == EventType.SLACKING else "N/A",
                        "reason": "模拟走神事件",
                        "duration_sec": random.uniform(5, 60),
                    },
                )

            # 每 30 秒模拟一次状态切换
            if tick % 30 == 0:
                new_state = random.choice([
                    SystemState.RESTING,
                    SystemState.MONITORING,
                ])
                sim.set_state(new_state)

            time.sleep(1.0)

    except KeyboardInterrupt:
        pass

    sim.stop()
    print("\n模拟演示结束。")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FocusFlow Lite BLE 通信模块")
    parser.add_argument("--demo", action="store_true", default=True,
                        help="运行模拟器演示")
    args = parser.parse_args()

    demo_simulator()
