#!/usr/bin/env python3
"""
FocusFlow Lite — UNO Q 端 BLE GATT Server 模板
==============================================

这是给 C 同学的模板代码，运行在 UNO Q Linux 侧。
笔记本通过蓝牙连上这个 Server，收发数据。

C 同学需要做的事:
  1. 在 UNO Q Linux 上安装 bleak:  pip install bleak
  2. 把这个文件拷到 UNO Q 上
  3. 运行: python ble_server.py
  4. 笔记本端运行 ble_demo.py --real 连接测试

本文件是模板，C 同学需要根据实际情况:
  - 对接脑电头环的 BLE 数据
  - 对接 ONNX 融合推理模型
  - 对接 STM32 RPC (OLED 更新)
  - 对接 ESP32 手环 BLE (振动指令)

作者: D 同学 (Tony) — 模板提供
日期: 2026-07-20
"""

import asyncio
import json
import time
import logging
import struct
from typing import Optional

# ===================================================================
# 第 0 步: 确认 bleak 已安装
# ===================================================================
try:
    from bleak import BleakServer, BleakGATTService, BleakGATTCharacteristic
    from bleak.uuids import normalize_uuid_str
    BLEAK_AVAILABLE = True
except ImportError:
    print("❌ 请先安装 bleak: pip install bleak")
    print("   UNO Q Linux 上执行: pip3 install bleak")
    BLEAK_AVAILABLE = False

if not BLEAK_AVAILABLE:
    exit(1)

# ===================================================================
# 日志
# ===================================================================
logging.basicConfig(
    level=logging.INFO,
    format="[UNO-Q] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ble_server")

# ===================================================================
# 第 1 步: 定义 UUID (和笔记本端完全一致)
# ===================================================================
SERVICE_UUID    = "0000FF00-0000-1000-8000-00805F9B34FB"
CHAR_LAPTOP_TX  = "0000FF01-0000-1000-8000-00805F9B34FB"  # 笔记本写入数据到这里
CHAR_UNO_TX     = "0000FF02-0000-1000-8000-00805F9B34FB"  # UNO Q 通知笔记本
CHAR_STATE_SYNC = "0000FF03-0000-1000-8000-00805F9B34FB"  # 双向状态同步
CHAR_HEARTBEAT  = "0000FF04-0000-1000-8000-00805F9B34FB"  # 笔记本心跳

DEVICE_NAME = "UNO-Q-FF01"

# ===================================================================
# 第 2 步: 数据处理回调 (C 同学在这里填自己的逻辑)
# ===================================================================

# 心跳超时检测
_last_heartbeat_time = time.time()
HEARTBEAT_TIMEOUT = 3.0  # 3 秒没心跳就认为笔记本断开


def on_sensor_data_received(packet: dict):
    """
    收到笔记本发来的传感器数据 (每秒 1 次)。

    packet 内容:
      {
        "payload": {
          "eye": {
            "yaw": 5.2,         # 偏航角
            "pitch": -3.1,      # 俯仰角
            "is_focused": 1,    # 是否专注 (0/1)
            "state_duration": 12.5,  # 状态持续秒数
            "confidence": 0.92  # 人脸置信度
          },
          "screen": {
            "state_code": 1.0,      # 屏幕状态: 0=离开, 0.3=摸鱼, 0.6=浏览, 1.0=专注
            "confidence": 0.88,     # 置信度
            "app_category": 3,      # 应用类别
            "state": "专注工作",
            "app": "VSCode"
          },
          "combined": {
            "overall_focus": 0.86   # 综合专注度
          }
        }
      }

    C 同学需要做的事:
      1. 提取 f_EYE(5维) + f_SCREEN(3维) = 8维
      2. 加上脑电头环的 f_EEG(5维) → 完整的 13 维
      3. 跑 ONNX 融合推理 → 输出走神概率
      4. 根据走神概率决策 → 是否触发反馈
    """
    eye = packet["payload"]["eye"]
    screen = packet["payload"]["screen"]

    # 提取 8 维特征
    f_eye = [
        eye["yaw"],
        eye["pitch"],
        eye["is_focused"],
        eye["state_duration"],
        eye["confidence"],
    ]
    f_screen = [
        screen["state_code"],
        screen["confidence"],
        screen["app_category"],
    ]

    logger.info(
        f"📊 传感器: 眼动(yaw={eye['yaw']:+.1f}°, focused={eye['is_focused']}) | "
        f"屏幕({screen['state']}, app={screen['app']}) | "
        f"综合专注={packet['payload']['combined']['overall_focus']:.2f}"
    )

    # ================================================================
    # TODO C 同学: 在这里做融合推理
    # ================================================================
    # eeg_features = get_eeg_from_headset()  # 脑电 5 维
    # feature_13d = f_eeg + f_eye + f_screen
    # distraction_prob = onnx_model.predict(feature_13d)
    #
    # if distraction_prob > 0.5:
    #     trigger_feedback(severity="high")


def on_distraction_event_received(packet: dict):
    """
    收到笔记本发来的走神事件 (检测到走神/摸鱼时即时发送)。

    C 同学需要做的事:
      根据 severity 选择反馈策略:
        - high   → 双振 + OLED 告警 + 笔记本弹窗
        - medium → 短振 + OLED 提示
        - low    → 仅 OLED 状态更新
    """
    payload = packet["payload"]
    event_type = payload["event_type"]
    severity = payload["severity"]
    source = payload["source"]
    details = payload.get("details", {})

    logger.warning(
        f"⚠️ 走神事件: {event_type} | "
        f"严重度={severity} | 来源={source} | "
        f"详情={details}"
    )

    # ================================================================
    # TODO C 同学: 在这里触发反馈
    # ================================================================
    # if severity == "high":
    #     vibrate_wristband("double_pulse")
    #     update_oled(["⚠️ 摸鱼提醒", f"应用: {details['app']}", "", "请回到工作"])
    #     notify_laptop("vibrate_and_display", ...)


def on_state_sync_received(packet: dict):
    """
    收到笔记本发来的状态同步。

    C 同学需要做的事:
      根据 system_state 更新 OLED 显示:
        - monitoring → 正常专注度界面
        - resting    → ☕ 休息中 + 倒计时
        - paused     → ⏸ 已暂停
        - error      → ⚠ 错误
    """
    payload = packet["payload"]
    state = payload["system_state"]
    sub = payload.get("sub_state", "")

    logger.info(f"📡 状态同步: {state}" + (f" ({sub})" if sub else ""))

    # ================================================================
    # TODO C 同学: 更新 OLED 显示
    # ================================================================
    # if state == "monitoring":
    #     rpc_send_stm32({"cmd": "oled", "line1": "FocusFlow Lite", ...})
    # elif state == "resting":
    #     rpc_send_stm32({"cmd": "oled", "line1": "☕ 休息中", ...})


# ===================================================================
# 第 3 步: GATT Server 实现 (使用 bleak)
# ===================================================================

class FocusFlowBLEServer:
    """
    UNO Q 端 BLE GATT Server。

    笔记本连上来之后:
      - 笔记本往 LAPTOP_TX 写数据 → on_laptop_tx_write() 处理
      - 笔记本往 HEARTBEAT 写心跳 → on_heartbeat_write() 更新计时
      - UNO Q 往 UNO_TX 通知数据 → notify_laptop() 发送
    """

    def __init__(self):
        self._server: Optional[BleakServer] = None
        self._connected_client = None
        self._running = False

    async def start(self):
        """启动 BLE GATT Server，等待笔记本连接。"""
        logger.info("正在启动 BLE GATT Server...")

        # 定义服务
        service = BleakGATTService(
            SERVICE_UUID,
            [
                # C1: 笔记本写入传感器数据 + 事件
                BleakGATTCharacteristic(
                    CHAR_LAPTOP_TX,
                    ["write"],
                    on_write=self._on_laptop_tx_write,
                ),
                # C2: UNO Q 通知笔记本 (反馈指令)
                BleakGATTCharacteristic(
                    CHAR_UNO_TX,
                    ["notify"],
                ),
                # C3: 双向状态同步
                BleakGATTCharacteristic(
                    CHAR_STATE_SYNC,
                    ["write", "notify"],
                    on_write=self._on_state_sync_write,
                ),
                # C4: 心跳
                BleakGATTCharacteristic(
                    CHAR_HEARTBEAT,
                    ["write"],
                    on_write=self._on_heartbeat_write,
                ),
            ],
        )

        # 创建并启动 Server
        self._server = BleakServer(
            device_name=DEVICE_NAME,
            services=[service],
        )

        await self._server.start()
        logger.info(f"✅ BLE Server 已启动: {DEVICE_NAME}")
        logger.info("   等待笔记本连接...")

        self._running = True

        # 心跳检测循环
        while self._running:
            await asyncio.sleep(1)
            if time.time() - _last_heartbeat_time > HEARTBEAT_TIMEOUT:
                logger.warning("📵 笔记本心跳超时 — 连接可能已断开")
                # TODO: OLED 显示 "笔记本已断开"

    async def stop(self):
        """停止 Server。"""
        self._running = False
        if self._server:
            await self._server.stop()
        logger.info("BLE Server 已停止")

    # ---- 写入回调 ----

    async def _on_laptop_tx_write(self, data: bytes):
        """
        笔记本往 LAPTOP_TX 写入数据时触发。
        数据可能是 sensor_data 或 distraction_event。
        """
        try:
            text = data.decode("utf-8")
            packet = json.loads(text)
            ptype = packet.get("type", "")

            if ptype == "sensor_data":
                on_sensor_data_received(packet)
            elif ptype == "distraction_event":
                on_distraction_event_received(packet)
            elif ptype == "event_ack":
                logger.debug(f"收到事件确认: {packet['payload'].get('event_id')}")
            else:
                logger.warning(f"未知数据包类型: {ptype}")

        except json.JSONDecodeError:
            logger.error(f"JSON 解析失败: {data[:100]}")
        except Exception as e:
            logger.error(f"数据处理异常: {e}")

    async def _on_state_sync_write(self, data: bytes):
        """笔记本往 STATE_SYNC 写入时触发。"""
        try:
            text = data.decode("utf-8")
            packet = json.loads(text)
            on_state_sync_received(packet)
        except Exception as e:
            logger.error(f"状态同步处理异常: {e}")

    async def _on_heartbeat_write(self, data: bytes):
        """笔记本往 HEARTBEAT 写入心跳时触发。"""
        global _last_heartbeat_time
        _last_heartbeat_time = time.time()
        # 心跳数据 1 byte (0-255 循环计数器)，这里只需要知道"有新数据"即可

    # ---- 发送给笔记本 ----

    async def notify_laptop(self, packet: dict):
        """
        通过 UNO_TX Notify 发送数据给笔记本。
        用于发送反馈指令 (feedback_cmd)。
        """
        if not self._server:
            return
        try:
            data = json.dumps(packet, ensure_ascii=False).encode("utf-8")
            await self._server.notify(CHAR_UNO_TX, data)
            logger.info(f"📤 已发送通知: {packet.get('type')}")
        except Exception as e:
            logger.error(f"通知发送失败: {e}")


# ===================================================================
# 第 4 步: 启动入口
# ===================================================================

def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║     FocusFlow Lite — UNO Q BLE Server                       ║
║     设备名: UNO-Q-FF01                                       ║
║     等待笔记本连接...                                         ║
╚══════════════════════════════════════════════════════════════╝
""")

    server = FocusFlowBLEServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("收到退出信号...")
        asyncio.run(server.stop())


if __name__ == "__main__":
    main()
