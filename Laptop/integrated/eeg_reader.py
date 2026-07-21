#!/usr/bin/env python3
"""
eeg_reader.py — Flowtime 脑电头环实时数据读取器 (AffectiveCloud 情感计算)

基于 Entertech 官方 SDK 示例编写。

架构：
  enterble (FlowtimeCollector)     ← BLE →  Flowtime 头环
  affectivecloud (ACClient)        ← WSS →  AffectiveCloud 云端

流程：
  1. FlowtimeCollector 扫描并连接 BLE 设备
  2. 收到 SOC (电量) 回调 → 触发 AffectiveCloud 会话创建
  3. EEG/HR 数据通过 BLE 回调接收 → upload_raw_data_to_device → 云端分析
  4. 云端返回专注度/放松度/压力等实时指标 → 终端实时显示

依赖：
  - enterble >= 1.1.6       (BLE 设备采集)
  - affectivecloud >= 1.2.9 (情感云 WebSocket)
  - bleak >= 0.19.0         (BLE 底层)

环境变量：
  APP_KEY / APP_SECRET / CLIENT_ID

用法：
  python eeg_reader.py
"""

import asyncio
import hashlib
import json
import datetime
import os
import sys
import time
import logging
import websockets
from pathlib import Path
from typing import Any, Optional, Tuple, Set

# ---------------------------------------------------------------------------
# 第三方库
# ---------------------------------------------------------------------------
from enterble import FlowtimeCollector

from affectivecloud import ACClient
from affectivecloud.algorithm import BaseServices, AffectiveServices
from affectivecloud.protocols import Services

from local_eeg_features import FeatureConfig, StreamingFeatureExtractor
from personal_eeg_model import PersonalEEGModel
from focus_decision import FocusDecisionEngine
from rest_control import RestController

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("eeg_reader")

# 降低 bleak 日志噪音
logging.getLogger("bleak").setLevel(logging.WARNING)
logging.getLogger("enterble").setLevel(logging.INFO)

# ---- 后台任务追踪 ----
_background_tasks: Set[asyncio.Task] = set()


def _create_tracked_task(coro) -> asyncio.Task:
    """创建后台任务并追踪, 防止 shutdown 时 'Task was destroyed but it is pending!' 警告"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
APP_KEY = os.environ.get("APP_KEY", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
_CLIENT_ID_RAW = os.environ.get("CLIENT_ID", "test_user_01")

# 如果使用了默认的测试 ID，给出提示
if not os.environ.get("CLIENT_ID"):
    logger.info("💡 未设置 CLIENT_ID 环境变量，使用测试 ID: test_user_01")

# AffectiveCloud 要求 user_id 必须为原始 ID 的 MD5 哈希值 (32位小写hex)
CLIENT_ID = hashlib.md5(_CLIENT_ID_RAW.encode()).hexdigest()
logger.info(f"🔑 CLIENT_ID: {_CLIENT_ID_RAW} → MD5 → {CLIENT_ID}")

# AffectiveCloud WebSocket 地址 (来自官方示例)
AC_WS_URL = "wss://server.affectivecloud.cn/ws/algorithm/v2/"

# Flowtime BLE 广播 UUID (来自官方 SDK)
MODEL_NBR_UUID = "0000ff10-1212-abcd-1523-785feabcd123"

# BLE 设备标识 (MAC 地址)
#   可通过环境变量 FLOWTIME_MAC 指定，留空则自动发现
DEVICE_IDENTIFY = os.environ.get("FLOWTIME_MAC", "") or None

# Dashboard WebSocket. 0.0.0.0 allows another computer on the same LAN to
# access the UNO Q. Set WS_HOST=127.0.0.1 if local-only access is preferred.
WS_HOST = os.environ.get("WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("WS_PORT", "8765"))

EEG_SAMPLE_RATE = 250
UPLOAD_CYCLE = 3
BLE_COLLECTOR_START_TIMEOUT = float(
    os.environ.get("BLE_COLLECTOR_START_TIMEOUT", "35")
)
BLE_CONNECT_MAX_RETRIES = int(
    os.environ.get("BLE_CONNECT_MAX_RETRIES", "2" if os.name == "nt" else "5")
)
# Raw EEG notifications normally arrive many times per second.  Treat a short
# gap as a warning, but do not write recovery commands to an unhealthy GATT
# link: that sequence has repeatedly made the UNO Q HCI controller disappear.
EEG_STALE_WARN_SECONDS = float(os.environ.get("EEG_STALE_WARN_SECONDS", "12"))
EEG_STALE_EXIT_SECONDS = float(os.environ.get("EEG_STALE_EXIT_SECONDS", "30"))
# On Windows the BLE link may stay connected while the headband's EEG
# producer becomes stuck after a brief contact interruption.  In that state
# HR/SOC packets still arrive and AffectiveCloud returns all-zero waveforms,
# so the raw-packet watchdog above cannot detect the fault.  A single
# START_EEG is the smallest recovery operation exposed by EnterBLE: it does
# not stop notifications or reconnect the device.
EEG_FLATLINE_RECOVERY_SECONDS = float(
    os.environ.get(
        "EEG_FLATLINE_RECOVERY_SECONDS",
        "10" if os.name == "nt" else "0",
    )
)
EEG_RECOVERY_WRITE_TIMEOUT = float(
    os.environ.get("EEG_RECOVERY_WRITE_TIMEOUT", "4")
)
EEG_RECOVERY_COOLDOWN_SECONDS = float(
    os.environ.get("EEG_RECOVERY_COOLDOWN_SECONDS", "45")
)
EEG_RECOVERY_MAX_ATTEMPTS_PER_INCIDENT = int(
    os.environ.get("EEG_RECOVERY_MAX_ATTEMPTS_PER_INCIDENT", "2")
)
AFFECTIVE_SERVICE_LIST = [
    AffectiveServices.ATTENTION,
]

SCRIPT_DIR = Path(__file__).resolve().parent
PERSONAL_MODEL_DIR = Path(
    os.environ.get(
        "EEG_MODEL_DIR",
        str(SCRIPT_DIR / "model_artifacts" / "eeg_attention_teacher_v4"),
    )
)


async def auto_discover_mac() -> Tuple[str, str]:
    """扫描附近 Flowtime BLE 设备，自动选择第一个设备

    持续重试直到发现设备。
    返回 (mac_address, device_name)。
    """
    from enterble.ble.scanner import DeviceScanner

    logger.info("🔍 扫描 Flowtime 设备 (BLE)...")
    logger.info(f"   广播 UUID: {MODEL_NBR_UUID}")
    logger.info("   （持续扫描中，请确保头环已开机...）")

    attempt = 0
    scan_failures = 0
    while True:
        attempt += 1
        try:
            devices = await DeviceScanner.discover(
                name=None,
                model_nbr_uuid=MODEL_NBR_UUID,
                timeout=5,
            )
        except Exception as e:
            # BlueZ can return InProgress if a previous scan hasn't fully
            # stopped yet, or if the adapter is in a transitional state
            # (e.g. recovering from a firmware crash). Back off and retry.
            scan_failures += 1
            wait = min(2 ** scan_failures, 60)  # exponential backoff, capped at 60s
            logger.warning(
                f"   ⚠️  第 {attempt} 次扫描失败: {e}"
            )
            logger.info(f"   🔄 {wait}s 后重试...")
            await asyncio.sleep(wait)
            continue

        # Reset backoff on successful scan (even if no devices found)
        scan_failures = 0

        if devices:
            print("\n" + "=" * 60)
            print("  发现以下 Flowtime 设备:")
            print("-" * 60)
            for i, d in enumerate(devices, 1):
                mac = d.identify
                name = d.device.name or "(未知名称)"
                rssi = getattr(d.device, "rssi", "?")
                print(f"    [{i}]  {name}")
                print(f"         MAC: {mac}   信号: {rssi} dBm")
            print("-" * 60)

            # 自动选择第一个 (信号最强的) 设备
            selected = devices[0]
            mac = selected.identify
            name = selected.device.name or "Flowtime"
            logger.info(f"   🤖 自动选择: {name} — {mac}")
            return mac, name

        # Some Windows advertisement cycles contain the device name but omit
        # the service UUID.  Retry once by the exact Flowtime name before
        # declaring the scan empty.
        try:
            name_only_devices = await DeviceScanner.discover(
                name="Flowtime Headband",
                model_nbr_uuid=None,
                timeout=5,
            )
        except Exception:
            name_only_devices = []
        if name_only_devices:
            def fallback_rssi(item) -> float:
                try:
                    return float(getattr(item.device, "rssi", None))
                except (TypeError, ValueError):
                    return float("-inf")

            selected = max(name_only_devices, key=fallback_rssi)
            mac = selected.identify
            name = selected.device.name or "Flowtime Headband"
            logger.info("   通过设备名称发现头环: %s — %s", name, mac)
            return mac, name

        logger.info(f"   ⏳ 第 {attempt} 次扫描未发现设备，继续重试...")
        await asyncio.sleep(2)


# ============================================================================
# AffectiveCloud 回调 — 服务生命周期管理
# ============================================================================

class AffectiveCloudSession:
    """管理 AffectiveCloud 会话生命周期 & 实时数据展示"""

    def __init__(self, client: ACClient):
        self.client = client
        self.session_created = False
        self.services_ready = False
        self.affective_ready = False

        # 统计
        self.eeg_samples_uploaded = 0
        self.hr_samples_uploaded = 0
        self.affective_updates = 0
        self.start_time: Optional[float] = None
        self.session_writer = None  # 由 main() 注入
        self.latest_eeg_quality = None
        self.local_feature_updates = 0
        self.personal_predictions = 0
        self.local_extractor = StreamingFeatureExtractor(
            FeatureConfig(sample_rate=EEG_SAMPLE_RATE)
        )
        # AffectiveCloud can keep returning EEG records while both waveform
        # channels are exactly zero.  Track that separately from a stopped BLE
        # notification stream so the Windows entry point can recover it.
        self.cloud_flatline_detected_at: Optional[float] = None
        self.cloud_flatline_started_ts: Optional[float] = None
        self.cloud_flatline_recovery_attempts = 0
        self.cloud_flatline_last_recovery_at = float("-inf")
        self.personal_model: Optional[PersonalEEGModel] = None
        self.smoothed_distraction: Optional[float] = None
        try:
            self.personal_model = PersonalEEGModel(PERSONAL_MODEL_DIR)
            logger.info(
                "🧠 已加载自研脑电模型: %s（五参数本地计算）",
                self.personal_model.model_name,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ 自研脑电模型未加载，将只输出本地五参数: %s",
                exc,
            )

        # 最近一次情感数据
        self.latest_attention = None

    # ---- 会话 ----

    async def on_session_create(self, resp):
        """会话创建成功 → 初始化基础服务"""
        code = getattr(resp, "code", -1)
        if code == 0:
            sid = getattr(resp, "session_id", "?")
            logger.info(f"✅ 会话已创建 (id={sid[:16]}...)")
            self.session_created = True
            self.services_ready = False
            self.affective_ready = False
            # Keep one continuous recording timeline if the cloud session has
            # to be recreated after a temporary data/network interruption.
            if self.start_time is None:
                self.start_time = time.time()
            await self.client.init_base_services(services=[
                BaseServices.EEG,
                BaseServices.HR,
            ])
        else:
            msg = getattr(resp, "msg", "?")
            logger.error(f"❌ 会话创建失败: code={code} msg={msg}")

    async def on_session_restore(self, resp):
        code = getattr(resp, "code", -1)
        sid = getattr(resp, "session_id", "?")
        logger.info(f"🔄 会话恢复: code={code} id={sid}")

    async def on_session_close(self, resp):
        self.session_created = False
        self.services_ready = False
        self.affective_ready = False
        logger.info("🔒 会话已关闭")

    # ---- 基础服务 ----

    async def on_base_service_init(self, resp):
        """基础服务初始化成功 → 订阅数据 + 启动情感计算"""
        code = getattr(resp, "code", -1)
        if code == 0:
            data = getattr(resp, "data", {})
            logger.info(f"✅ 基础服务已初始化: {data}")
            # 订阅基础数据
            await self.client.subscribe_base_services(services=[
                BaseServices.EEG,
                BaseServices.HR,
            ])
            # 启动情感计算服务
            await self.client.start_affective_services(services=AFFECTIVE_SERVICE_LIST)

    async def on_base_service_subscribe(self, resp):
        """基础数据订阅回调 — 接收云端解析后的 EEG 波形 & HR 数据"""
        data = getattr(resp, "data", None)
        if data is None:
            return
        response_type = getattr(resp, "response_type", None)
        if response_type == 0:  # 订阅状态
            logger.info(f"📋 基础数据订阅确认: {list(data.keys())}")
        elif response_type == 1:  # 实时数据
            ts = round(time.time() - (self.start_time or time.time()), 2)

            # ---- EEG 波形数据 ----
            if "eeg" in data:
                eeg_block = data["eeg"]
                eegl = eeg_block.get("eegl_wave", [])
                eegr = eeg_block.get("eegr_wave", [])
                eeg_quality = _first_present(
                    eeg_block,
                    "eeg_quality",
                    "quality",
                    "signal_quality",
                )
                if eeg_quality is None:
                    eeg_quality = _first_present(
                        data,
                        "eeg_quality",
                        "quality",
                        "signal_quality",
                    )
                msg = {
                    "type": "eeg",
                    "ts": ts,
                    "sample_rate": EEG_SAMPLE_RATE,
                    "eegl": eegl,
                    "eegr": eegr,
                    "eeg_alpha_power": eeg_block.get("eeg_alpha_power", 0),
                    "eeg_beta_power": eeg_block.get("eeg_beta_power", 0),
                    "eeg_theta_power": eeg_block.get("eeg_theta_power", 0),
                    "eeg_delta_power": eeg_block.get("eeg_delta_power", 0),
                    "eeg_gamma_power": eeg_block.get("eeg_gamma_power", 0),
                }
                if eeg_quality is not None:
                    msg["eeg_quality"] = eeg_quality
                    self.latest_eeg_quality = eeg_quality
                    _latest_status["eeg_quality"] = eeg_quality

                waveform = list(eegl or []) + list(eegr or [])
                is_all_zero = bool(waveform) and all(
                    float(value) == 0.0 for value in waveform
                )
                if is_all_zero:
                    if self.cloud_flatline_detected_at is None:
                        self.cloud_flatline_detected_at = time.monotonic()
                        self.cloud_flatline_started_ts = ts
                elif self.cloud_flatline_detected_at is not None:
                    flatline_seconds = (
                        time.monotonic() - self.cloud_flatline_detected_at
                    )
                    recovery_was_requested = self.cloud_flatline_recovery_attempts > 0
                    if recovery_was_requested:
                        logger.info(
                            "✅ EEG 波形已恢复，平线持续 %.1f 秒",
                            flatline_seconds,
                        )
                    else:
                        logger.info(
                            "✅ EEG 波形开始有效，启动平线持续 %.1f 秒",
                            flatline_seconds,
                        )
                    recovery_msg = {
                        "type": "eeg_stream_state",
                        "ts": ts,
                        "state": (
                            "flatline_recovered"
                            if recovery_was_requested
                            else "flatline_cleared"
                        ),
                        "duration_seconds": round(flatline_seconds, 2),
                        "recovery_was_requested": recovery_was_requested,
                    }
                    if self.session_writer:
                        self.session_writer.write(recovery_msg)
                    _create_tracked_task(ws_broadcast(recovery_msg))
                    self.cloud_flatline_detected_at = None
                    self.cloud_flatline_started_ts = None
                    # A later movement is a new incident and may receive its
                    # own bounded recovery attempts.
                    self.cloud_flatline_recovery_attempts = 0

                self.eeg_samples_uploaded += len(eegl) + len(eegr)
                # Keep the BLE/cloud transport alive during rest so the
                # Flowtime link remains stable, but do not expose, persist or
                # classify physiological samples until monitoring resumes.
                if not is_resting():
                    if self.session_writer:
                        self.session_writer.write(msg)
                    _create_tracked_task(ws_broadcast(msg))
                    await self._process_personal_eeg(
                        eegl,
                        eegr,
                        quality=eeg_quality,
                        block_ts=ts,
                    )

            # ---- 心率数据 ----
            if "hr-v2" in data:
                hr_block = data["hr-v2"]
                msg = {
                    "type": "hr",
                    "ts": ts,
                    "hr": hr_block.get("hr", 0),
                    "hrv": hr_block.get("hrv", 0.0),
                }
                self.hr_samples_uploaded += 1
                if not is_resting():
                    if self.session_writer:
                        self.session_writer.write(msg)
                    _create_tracked_task(ws_broadcast(msg))

    async def _process_personal_eeg(
        self,
        eegl,
        eegr,
        *,
        quality,
        block_ts: float,
    ) -> None:
        """Compute our five EEG features and personalized model prediction.

        The official SDK only supplies the waveform. Detrending, FFT band
        integration, ratios, preprocessing and classifier inference are local.
        """
        try:
            quality_value = int(quality) if quality is not None else None
        except (TypeError, ValueError):
            quality_value = None

        features = self.local_extractor.add_block(
            eegl,
            eegr,
            quality=quality_value,
        )
        for index, feature in enumerate(features):
            feature_ts = block_ts - (
                len(features) - index - 1
            ) * self.local_extractor.config.step_seconds
            feature_msg = {
                "type": "eeg_features",
                "ts": round(max(0.0, feature_ts), 2),
                "source": "custom_fft_from_official_waveform",
                **feature.to_dict(),
            }
            self.local_feature_updates += 1
            if self.session_writer:
                self.session_writer.write(feature_msg)
            await ws_broadcast(feature_msg)

            if not feature.valid:
                _focus_engine.update_eeg(
                    None,
                    valid=False,
                    source="personal_model",
                    reason=feature.invalid_reason or "invalid_eeg_window",
                )
                await publish_focus_decision(ts=feature_msg["ts"])
                continue

            if self.personal_model is None:
                continue

            prediction = self.personal_model.predict(feature_msg)
            raw_distraction = prediction.distraction_probability
            if self.smoothed_distraction is None:
                self.smoothed_distraction = raw_distraction
            else:
                # Light smoothing prevents the demo state from flickering every
                # second while preserving the model's actual probability.
                self.smoothed_distraction = (
                    0.30 * raw_distraction
                    + 0.70 * self.smoothed_distraction
                )
            state = (
                "distraction"
                if self.smoothed_distraction >= prediction.threshold
                else "focus"
            )
            prediction_msg = {
                "type": "eeg_prediction",
                "ts": feature_msg["ts"],
                "source": "personal_model",
                "model": self.personal_model.model_name,
                "preliminary": True,
                **prediction.to_dict(),
                "smoothed_distraction_probability": self.smoothed_distraction,
                "smoothed_focus_probability": 1.0 - self.smoothed_distraction,
                "state": state,
            }
            self.personal_predictions += 1
            if self.session_writer:
                self.session_writer.write(prediction_msg)
            await ws_broadcast(prediction_msg)
            _focus_engine.update_eeg(
                100.0 * (1.0 - self.smoothed_distraction),
                valid=True,
                source=self.personal_model.model_name,
            )
            await publish_focus_decision(ts=feature_msg["ts"])

    async def on_base_service_report(self, resp):
        code = getattr(resp, "code", -1)
        if code == 0:
            logger.info("📊 基础服务报表已就绪")

    # ---- 情感计算服务 ----

    async def on_affective_service_start(self, resp):
        """情感计算启动 → 订阅情感数据"""
        code = getattr(resp, "code", -1)
        if code == 0:
            logger.info("✅ 情感计算服务已启动")
            await self.client.subscribe_affective_services(services=AFFECTIVE_SERVICE_LIST)

    async def on_affective_service_subscribe(self, resp):
        """情感计算数据回调"""
        data = getattr(resp, "data", None)
        if data is None:
            return
        response_type = getattr(resp, "response_type", None)
        if response_type == 0:  # 订阅状态
            logger.info(f"📋 情感数据订阅确认: {list(data.keys())}")
            self.affective_ready = True
            print_ready_banner()
        elif response_type == 1:  # 实时情感数据
            self.affective_updates += 1
            self._update_latest(data)
            self._print_realtime(data)

    async def on_affective_service_report(self, resp):
        code = getattr(resp, "code", -1)
        if code == 0:
            logger.info("📊 情感计算报表已就绪")

    async def on_affective_service_finish(self, resp):
        logger.info("🏁 情感计算服务已结束")

    def _update_latest(self, data: dict):
        """缓存唯一保留的官方专注度参考指标。"""
        for key, value in data.items():
            key_lower = key.lower()
            if "attention" in key_lower:
                self.latest_attention = value

    def _print_realtime(self, data: dict):
        """格式化输出实时情感数据 — 并广播到 WebSocket"""
        if is_resting():
            return
        parts = []
        ws_data = {"type": "affective", "ts": round(time.time() - (self.start_time or time.time()), 2)}

        for key, value in data.items():
            key_short = key.replace("sub_", "").replace("_fields", "")
            # 情感数据通常是 dict 或 list
            if isinstance(value, dict):
                for k, v in value.items():
                    ws_data[k] = v
                    if isinstance(v, (int, float)):
                        parts.append(f"{k}:{v:.2f}")
                    else:
                        parts.append(f"{k}:{v}")
            elif isinstance(value, (int, float)):
                ws_data[key_short] = value
                parts.append(f"{key_short}:{value:.2f}")
            elif isinstance(value, list) and len(value) <= 10:
                parts.append(f"{key_short}:[{','.join(f'{x:.1f}' if isinstance(x, float) else str(x) for x in value)}]")

        # 广播到前端 & 保存到文件
        _create_tracked_task(ws_broadcast(ws_data))
        if self.session_writer:
            self.session_writer.write(ws_data)

        # If the local model is unavailable, retain the official attention
        # value as an explicitly labelled fallback EEG percentage.  When the
        # local model is loaded it remains the only final-stage EEG source.
        if self.personal_model is None:
            attention = _first_present(ws_data, "attention", "Attention")
            if isinstance(attention, (int, float)):
                _focus_engine.update_eeg(
                    float(attention),
                    valid=True,
                    source="official_attention_fallback",
                )
                _create_tracked_task(publish_focus_decision(ts=ws_data["ts"]))

        if parts:
            elapsed = time.time() - (self.start_time or time.time())
            bar = _make_bar(data)
            line = f"\r⏱ {elapsed:5.0f}s | {' | '.join(parts)} {bar}"
            sys.stdout.write(line[:160])
            sys.stdout.flush()

    def summary(self) -> str:
        elapsed = time.time() - (self.start_time or time.time())
        return (
            f"运行 {elapsed:.0f}s | "
            f"EEG 样本: {self.eeg_samples_uploaded} | "
            f"本地五参数: {self.local_feature_updates} | "
            f"自研推理: {self.personal_predictions} | "
            f"官方参考更新: {self.affective_updates}"
        )


def _make_bar(data: dict) -> str:
    """绘制简易 ASCII 柱状图 (专注度 / 放松度)"""
    bars = []
    for key, value in data.items():
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, (int, float)) and v >= 0:
                    width = max(1, int(v / 5))  # 0-100 → 0-20 chars
                    bars.append(f"{k} [{'█' * min(width, 20):20s}] {v:5.1f}")
    return "  " + "  ".join(bars[-2:]) if bars else ""


def print_banner():
    print()
    print("╔" + "═" * 58 + "╗")
    print("║" + "  🧠 Flowtime 脑电头环 — AffectiveCloud 情感计算  ".center(52) + "║")
    print("╠" + "═" * 58 + "╣")
    print(f"║  APP_KEY:  {APP_KEY[:16]}...{' ' * (58 - 39)}║")
    print(f"║  Client:   {CLIENT_ID}{' ' * (58 - 20 - len(CLIENT_ID))}║")
    print("╚" + "═" * 58 + "╝")
    print()


def print_ready_banner():
    print()
    print("╔" + "═" * 58 + "╗")
    print("║" + "  ✅ 服务就绪 — 等待脑电数据...".ljust(52) + "║")
    print("╠" + "═" * 58 + "╣")
    print("║  专注度 (Attention)   |  放松度 (Relaxation)        ║")
    print("║  压力   (Pressure)    |  愉悦度 (Pleasure)          ║")
    print("║  激活度 (Arousal)     |  和谐度 (Coherence)         ║")
    print("╚" + "═" * 58 + "╝")
    print()


# ============================================================================
# 构建 ACClient 回调表
# ============================================================================

def build_recv_callbacks(session: AffectiveCloudSession) -> dict:
    """构建 ACClient 回调注册表 (与官方示例一致)"""
    return {
        Services.Type.SESSION: {
            Services.Operation.Session.CREATE: session.on_session_create,
            Services.Operation.Session.RESTORE: session.on_session_restore,
            Services.Operation.Session.CLOSE: session.on_session_close,
        },
        Services.Type.BASE_SERVICE: {
            Services.Operation.BaseService.INIT: session.on_base_service_init,
            Services.Operation.BaseService.SUBSCRIBE: session.on_base_service_subscribe,
            Services.Operation.BaseService.REPORT: session.on_base_service_report,
        },
        Services.Type.AFFECTIVE_SERVICE: {
            Services.Operation.AffectiveService.START: session.on_affective_service_start,
            Services.Operation.AffectiveService.SUBSCRIBE: session.on_affective_service_subscribe,
            Services.Operation.AffectiveService.REPORT: session.on_affective_service_report,
            Services.Operation.AffectiveService.FINISH: session.on_affective_service_finish,
        },
    }


# ============================================================================
# 主程序 — BLE 采集 + 云端分析
# ============================================================================

class SessionWriter:
    """Crash-tolerant JSON Lines writer.

    Each line is a complete JSON object, so a power loss can damage at most
    the final line instead of invalidating the whole recording.
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._file = open(filepath, "w", encoding="utf-8", buffering=1)
        self._closed = False

    def write(self, msg: dict):
        if self._closed:
            return
        self._file.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._file.flush()
        self._file.close()
        logger.info(f"📁 会话数据已保存: {self.filepath}")


# WebSocket 连接池
_ws_clients: Set[websockets.WebSocketServerProtocol] = set()
_session_writer_for_ws: Optional[SessionWriter] = None
_session_clock = lambda: 0.0
_focus_engine = FocusDecisionEngine()
_last_focus_decision_signature = None
_latest_focus_decision: Optional[dict] = None
_rest_controller = RestController()
_latest_status: dict = {
    "type": "status",
    "ble_connected": False,
    "is_worn": None,
    "battery": None,
}


def _first_present(mapping: dict, *keys: str) -> Any:
    """Return the first non-None value for a list of possible SDK field names."""
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def is_resting() -> bool:
    """Return whether sensing outputs are currently muted for a rest period."""
    return _rest_controller.active


def rest_snapshot() -> dict:
    return _rest_controller.snapshot()


def latest_focus_decision() -> Optional[dict]:
    return dict(_latest_focus_decision) if _latest_focus_decision is not None else None


def latest_screen_context() -> dict:
    return _focus_engine.latest_screen_context()


async def _publish_rest_state(message: dict) -> dict:
    message = {**message, "ts": round(_session_clock(), 2)}
    if _session_writer_for_ws:
        _session_writer_for_ws.write(message)
    await ws_broadcast(message)
    return message


async def handle_rest_command(incoming: dict) -> dict:
    """Apply a dashboard rest command and broadcast the authoritative state."""
    global _last_focus_decision_signature
    data = incoming.get("data")
    if not isinstance(data, dict):
        data = incoming
    action = str(data.get("action", "query")).strip().lower()
    if action == "start":
        state = _rest_controller.start(
            int(data.get("duration_seconds", data.get("duration", 300))),
            str(data.get("reason", "manual")),
        )
        _focus_engine.reset()
        _last_focus_decision_signature = None
    elif action == "stop":
        state = _rest_controller.stop()
        _focus_engine.reset()
        _last_focus_decision_signature = None
    elif action == "extend":
        state = _rest_controller.extend(
            int(data.get("duration_seconds", data.get("duration", 60)))
        )
    elif action == "query":
        state = _rest_controller.snapshot()
    else:
        raise ValueError(f"unknown rest action: {action}")
    state["event"] = action
    return await _publish_rest_state(state)


async def rest_state_loop() -> None:
    """Publish the rest countdown and automatically resume at zero."""
    global _last_focus_decision_signature
    while True:
        state, ended = _rest_controller.poll()
        if state["active"] or ended:
            if ended:
                state["event"] = "finished"
                _focus_engine.reset()
                _last_focus_decision_signature = None
            else:
                state["event"] = "tick"
            await _publish_rest_state(state)
        await asyncio.sleep(1.0)

async def ws_handler(websocket):
    """WebSocket 连接处理 — 每个浏览器连接一个实例"""
    _ws_clients.add(websocket)
    try:
        await websocket.send(json.dumps(_latest_status, ensure_ascii=False))
        await websocket.send(json.dumps(_rest_controller.snapshot(), ensure_ascii=False))
        async for raw in websocket:
            try:
                incoming = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            incoming_type = incoming.get("type")
            if incoming_type == "marker":
                label = str(incoming.get("label", "")).strip()[:64]
                if not label:
                    continue
                marker = {
                    "type": "marker",
                    "ts": round(_session_clock(), 2),
                    "label": label,
                }
                if _session_writer_for_ws:
                    _session_writer_for_ws.write(marker)
                await ws_broadcast(marker)
            elif incoming_type == "rest_command":
                try:
                    await handle_rest_command(incoming)
                except (TypeError, ValueError) as exc:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "code": "INVALID_REST_COMMAND",
                        "message": str(exc),
                    }, ensure_ascii=False))
            elif incoming_type in {"screen_state", "screen_data"}:
                payload = incoming.get("data")
                if not isinstance(payload, dict):
                    payload = incoming
                context_msg = {
                    "type": "screen_state",
                    "ts": round(_session_clock(), 2),
                    **{key: value for key, value in payload.items() if key != "type"},
                }
                _focus_engine.update_screen(context_msg)
                if _session_writer_for_ws:
                    _session_writer_for_ws.write(context_msg)
                await ws_broadcast(context_msg)
                await publish_focus_decision(ts=context_msg["ts"])
            elif incoming_type in {"camera_state", "eye_data"}:
                payload = incoming.get("data")
                if not isinstance(payload, dict):
                    payload = incoming
                context_msg = {
                    "type": "camera_state",
                    "ts": round(_session_clock(), 2),
                    **{key: value for key, value in payload.items() if key != "type"},
                }
                _focus_engine.update_camera(context_msg)
                if _session_writer_for_ws:
                    _session_writer_for_ws.write(context_msg)
                await ws_broadcast(context_msg)
                await publish_focus_decision(ts=context_msg["ts"])
    finally:
        _ws_clients.discard(websocket)

async def ws_broadcast(msg: dict):
    """向所有连接的 WebSocket 客户端广播 JSON 消息"""
    if not _ws_clients:
        return
    payload = json.dumps(msg)
    await asyncio.gather(
        *(client.send(payload) for client in _ws_clients),
        return_exceptions=True,
    )


async def publish_focus_decision(*, ts: float) -> dict:
    """Evaluate, persist and broadcast the user-facing hierarchical state."""
    global _last_focus_decision_signature, _latest_focus_decision
    if is_resting():
        rest = _rest_controller.snapshot()
        decision = {
            "type": "focus_decision",
            "ts": round(float(ts), 2),
            "state": "resting",
            "label": "休息中",
            "focus_percent": None,
            "reason": f"剩余 {rest['remaining_seconds']} 秒，监测输出已暂停",
        }
    else:
        decision = _focus_engine.evaluate(ts=ts)
    _latest_focus_decision = dict(decision)
    signature = (
        decision.get("state"),
        decision.get("label"),
        decision.get("focus_percent"),
        decision.get("reason"),
    )
    if signature == _last_focus_decision_signature:
        return decision
    _last_focus_decision_signature = signature
    if _session_writer_for_ws:
        _session_writer_for_ws.write(decision)
    await ws_broadcast(decision)
    return decision

async def start_ws_server():
    """启动 WebSocket 服务器 (后台任务)"""
    logger.info(f"🌐 WebSocket 服务启动: ws://{WS_HOST}:{WS_PORT}")
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        await asyncio.Future()  # 永久运行


async def main():
    """主入口：启动 BLE 采集器 → 连接云端 → 实时数据处理"""

    # ---- 0. 环境检查 ----
    if not all([APP_KEY, APP_SECRET, CLIENT_ID]):
        logger.error("❌ 环境变量未设置！")
        logger.error("   export APP_KEY='...'")
        logger.error("   export APP_SECRET='...'")
        logger.error("   export CLIENT_ID='...'")
        sys.exit(1)

    print_banner()

    # ---- 1. 准备会话管理器 (先占位, 等 client 创建后补全) ----
    global _session_writer_for_ws, _session_clock

    session = AffectiveCloudSession(None)  # client 引用稍后补全

    # ---- 2. 创建 ACClient (回调在 build_recv_callbacks 中绑定到 session) ----
    client = ACClient(
        url=AC_WS_URL,
        app_key=APP_KEY,
        secret=APP_SECRET,
        client_id=CLIENT_ID,
        upload_cycle=UPLOAD_CYCLE,
        recv_mode=ACClient.RecvMode.CALLBACK,
        recv_callbacks=build_recv_callbacks(session),
        ping_interval=20,
        ping_timeout=20,
        timeout=15,
        reconnect=True,
        reconnect_interval=5,
    )

    # 补全 session 中的 client 引用
    session.client = client

    # ---- 2. 自动发现设备 MAC (如果未指定) ----
    device_mac = DEVICE_IDENTIFY
    device_name = None  # 从扫描中获取的设备名, 传给 FlowtimeCollector
    if device_mac is None:
        device_mac, device_name = await auto_discover_mac()
        if device_mac is None:
            logger.error("❌ 未发现 Flowtime 设备！")
            logger.info("")
            logger.info("💡 排查建议：")
            logger.info("   1. 确认头环已开机（指示灯亮起）")
            logger.info("   2. 确认头环在蓝牙范围内（< 10 米）")
            logger.info("   3. 可通过 FLOWTIME_MAC 环境变量手动指定 MAC 地址")
            sys.exit(1)

    # ---- 3. BLE 数据回调定义 ----
    ws_scheduled = False
    cloud_create_requested_at = 0.0
    disconnect_event = asyncio.Event()
    last_raw_eeg_at = time.monotonic()
    raw_eeg_packets = 0
    raw_stale_reported = False
    ble_fault_exit = False
    stop_reason = ""
    shutting_down = False
    upload_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    async def publish_status(**updates):
        """Record and broadcast device state changes."""
        _latest_status.update(updates)
        msg = dict(_latest_status)
        msg["type"] = "status"
        msg["ts"] = round(time.time() - (session.start_time or time.time()), 2)
        if session.session_writer:
            session.session_writer.write(msg)
        await ws_broadcast(msg)

    async def on_soc(soc_percentage: float):
        """电量回调 — 首次触发时建立云端连接"""
        nonlocal ws_scheduled, cloud_create_requested_at
        logger.info(f"🔋 电量: {soc_percentage:.0f}%")
        await publish_status(battery=round(soc_percentage, 1), ble_connected=True)
        if not ws_scheduled and not session.session_created:
            ws_scheduled = True
            logger.info("🌐 连接 AffectiveCloud...")
            try:
                ws_open = client.ws is not None and not getattr(client.ws, "closed", False)
                if not ws_open:
                    client.closed = False
                    await client.connect()
            except Exception as exc:
                ws_scheduled = False
                logger.error(f"❌ AffectiveCloud 连接失败: {exc}")
                return
            # client.connect() 内部用 ensure_future 异步执行,
            # 需等待 WebSocket 真正建立后再创建会话
            for _ in range(50):  # 最多等 10 秒
                if client.ws is not None and not getattr(client.ws, "closed", False):
                    break
                await asyncio.sleep(0.2)
            if client.ws is None or getattr(client.ws, "closed", False):
                logger.error("❌ WebSocket 连接超时!")
                ws_scheduled = False
                return
            logger.info("📝 创建会话...")
            cloud_create_requested_at = time.monotonic()
            try:
                await client.create_session()
            except Exception as exc:
                ws_scheduled = False
                logger.error(f"❌ 创建云端会话失败: {exc}")

    _last_wear_status = None

    async def on_wear_status(is_worn: bool):
        """穿戴状态回调 — 仅状态变化时打印"""
        nonlocal _last_wear_status, last_raw_eeg_at
        if is_worn != _last_wear_status:
            _last_wear_status = is_worn
            # A wear transition is a new contact incident.  Discard an old
            # all-zero timer and its bounded attempt count; the next cloud EEG
            # block will establish whether the newly worn signal is flat.
            session.cloud_flatline_detected_at = None
            session.cloud_flatline_started_ts = None
            session.cloud_flatline_recovery_attempts = 0
            if is_worn:
                # Give the EEG notification stream a grace period after wearing.
                last_raw_eeg_at = time.monotonic()
            status_text = "已佩戴 ✅" if is_worn else "未佩戴 ⚠️"
            logger.info(f"👤 穿戴状态: {status_text}")
            await publish_status(is_worn=is_worn, ble_connected=True)

    async def on_eeg_data(data: tuple):
        """EEG 数据回调 — 20 字节原始数据 → 上传云端"""
        nonlocal last_raw_eeg_at, raw_eeg_packets, raw_stale_reported
        last_raw_eeg_at = time.monotonic()
        raw_eeg_packets += 1
        if raw_stale_reported:
            raw_stale_reported = False
            await publish_status(data_stale=False, ble_connected=True)
        # Never await network I/O in Bleak's notification callback. A delayed
        # cloud send would otherwise block subsequent BLE notifications.
        try:
            upload_queue.put_nowait((BaseServices.EEG, list(data)))
        except asyncio.QueueFull:
            try:
                upload_queue.get_nowait()
                upload_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            upload_queue.put_nowait((BaseServices.EEG, list(data)))

    async def on_hr_data(data: int):
        """心率数据回调"""
        try:
            upload_queue.put_nowait((BaseServices.HR, [data]))
        except asyncio.QueueFull:
            logger.warning("⚠️ 云端上传队列已满，丢弃一个心率包")

    async def on_device_disconnected(device):
        """设备断开回调 — 记录故障并安全退出，不在原进程内重扫。"""
        nonlocal ble_fault_exit, stop_reason
        logger.warning("⚠️ 设备已断开！")
        ble_fault_exit = True
        stop_reason = "device_disconnected"
        await publish_status(ble_connected=False, is_worn=False)
        disconnect_event.set()

    async def cloud_upload_worker():
        """Upload queued BLE packets without blocking the BLE callback loop."""
        while not shutting_down:
            service, values = await upload_queue.get()
            try:
                if session.session_created:
                    await client.upload_raw_data_from_device({service: values})
            except Exception as exc:
                logger.warning(f"⚠️ 云端原始数据上传失败: {exc}")
            finally:
                upload_queue.task_done()

    async def health_watchdog():
        """Detect a silent EEG stream without touching an unhealthy GATT link."""
        nonlocal raw_stale_reported, ws_scheduled, ble_fault_exit, stop_reason
        while not shutting_down:
            await asyncio.sleep(2)

            # A failed/closed cloud session may leave the SDK socket reconnected
            # without creating a new session. Allow the next SOC callback to retry.
            if (
                ws_scheduled
                and not session.session_created
                and cloud_create_requested_at
                and time.monotonic() - cloud_create_requested_at > 12
            ):
                ws_scheduled = False

            if _last_wear_status is not True or disconnect_event.is_set():
                continue

            # A short loss of electrode contact can leave firmware 3.0.6
            # delivering packets while the EEG payload seen through the
            # official cloud service remains all zero.  The ordinary BLE
            # watchdog cannot see this because notifications are still alive.
            # On Windows, request START_EEG only; never stop notifications or
            # reconnect the device here.
            now = time.monotonic()
            flatline_started = session.cloud_flatline_detected_at
            if (
                EEG_FLATLINE_RECOVERY_SECONDS > 0
                and flatline_started is not None
                and now - flatline_started >= EEG_FLATLINE_RECOVERY_SECONDS
                and (
                    now - session.cloud_flatline_last_recovery_at
                    >= EEG_RECOVERY_COOLDOWN_SECONDS
                )
                and (
                    session.cloud_flatline_recovery_attempts
                    < EEG_RECOVERY_MAX_ATTEMPTS_PER_INCIDENT
                )
            ):
                session.cloud_flatline_recovery_attempts += 1
                session.cloud_flatline_last_recovery_at = now
                attempt = session.cloud_flatline_recovery_attempts
                logger.warning(
                    "🔧 EEG 包仍在到达但云端波形持续全 0；补发 START_EEG "
                    "(0x01)，不停止通知、不重连（第 %d/%d 次）",
                    attempt,
                    EEG_RECOVERY_MAX_ATTEMPTS_PER_INCIDENT,
                )
                recovery_msg = {
                    "type": "eeg_stream_state",
                    "ts": round(
                        time.time()
                        - (session.start_time or recording_started_at),
                        2,
                    ),
                    "state": "flatline_restart_requested",
                    "attempt": attempt,
                    "command": "START_EEG",
                }
                session_writer.write(recovery_msg)
                await ws_broadcast(recovery_msg)
                try:
                    await asyncio.wait_for(
                        collector.device.write_gatt_char(
                            FlowtimeCollector.DOWN_CODE_UUID,
                            bytes([FlowtimeCollector.DownCode.START_EEG]),
                            response=True,
                        ),
                        timeout=EEG_RECOVERY_WRITE_TIMEOUT,
                    )
                    logger.info(
                        "✅ START_EEG 已被头环接受；保持佩戴并等待波形恢复"
                    )
                    await publish_status(
                        eeg_recovery_attempts=attempt,
                        eeg_recovery_command="accepted_after_flatline",
                    )
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    logger.error(
                        "❌ START_EEG 平线恢复失败：%s；不执行断开或重连",
                        error,
                    )
                    session_writer.write({
                        "type": "eeg_stream_state",
                        "ts": round(
                            time.time()
                            - (session.start_time or recording_started_at),
                            2,
                        ),
                        "state": "flatline_restart_failed",
                        "attempt": attempt,
                        "error": error,
                    })
                    await publish_status(
                        eeg_recovery_attempts=attempt,
                        eeg_recovery_command="failed_after_flatline",
                    )

            silence = time.monotonic() - last_raw_eeg_at
            if silence > EEG_STALE_WARN_SECONDS and not raw_stale_reported:
                raw_stale_reported = True
                logger.warning(
                    f"⚠️ {silence:.0f} 秒未收到 BLE EEG 原始包；"
                    "保持连接并等待自行恢复，不发送 GATT 重启指令"
                )
                await publish_status(data_stale=True, ble_connected=True, reconnecting=False)

            # Give a transient radio/contact interruption time to recover. If
            # it does not, save and leave the process without STOP_ALL,
            # START_ALL or a fresh scan. The next run can reconnect if BlueZ
            # still exposes its controller.
            if silence > EEG_STALE_EXIT_SECONDS and not disconnect_event.is_set():
                ble_fault_exit = True
                stop_reason = "eeg_timeout"
                logger.error(
                    f"❌ {silence:.0f} 秒仍无 EEG 数据，保存会话并保护性退出"
                )
                logger.error("   不向异常 BLE 连接写入命令；退出后先检查 bluetoothctl show")
                disconnect_event.set()

    # ---- 4. 创建 FlowtimeCollector (BLE 采集器) ----

    # ---- 4.5 初始化会话记录 & WebSocket ----
    session_filepath = f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    session_writer = SessionWriter(session_filepath)
    session.session_writer = session_writer
    recording_started_at = time.time()
    _session_writer_for_ws = session_writer
    _session_clock = lambda: time.time() - (session.start_time or recording_started_at)
    session_writer.write({
        "type": "meta",
        "schema_version": 2,
        "started_at": datetime.datetime.now().astimezone().isoformat(),
        "eeg_sample_rate": EEG_SAMPLE_RATE,
        "upload_cycle": UPLOAD_CYCLE,
    })
    ws_server_task = _create_tracked_task(start_ws_server())
    rest_task = _create_tracked_task(rest_state_loop())
    health_task = _create_tracked_task(health_watchdog())
    upload_task = _create_tracked_task(cloud_upload_worker())

    def make_collector(mac: str, name: Optional[str]) -> FlowtimeCollector:
        return FlowtimeCollector(
            name=name,
            model_nbr_uuid=MODEL_NBR_UUID,
            device_identify=mac,
            device_disconnected_callback=on_device_disconnected,
            soc_data_callback=on_soc,
            wear_status_callback=on_wear_status,
            eeg_data_callback=on_eeg_data,
            hr_data_callback=on_hr_data,
        )

    collector = make_collector(device_mac, device_name)

    # ---- 5. 启动采集器 (含重试, 因为 BLE 连接可能偶发超时) ----
    max_retries = max(1, BLE_CONNECT_MAX_RETRIES)
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"⏳ 连接 Flowtime 设备... (第 {attempt}/{max_retries} 次)")
            await asyncio.wait_for(
                collector.start(),
                timeout=BLE_COLLECTOR_START_TIMEOUT,
            )
            await publish_status(ble_connected=True)
            break  # 成功, 跳出重试循环
        except asyncio.TimeoutError:
            logger.warning(
                "⚠️  Flowtime 连接/订阅超过 %.0f 秒，丢弃本次 Windows BLE "
                "对象并重新扫描",
                BLE_COLLECTOR_START_TIMEOUT,
            )
            if attempt < max_retries:
                new_mac, new_name = await auto_discover_mac()
                if new_mac:
                    device_mac, device_name = new_mac, new_name
                    collector = make_collector(device_mac, device_name)
                await asyncio.sleep(2)
            else:
                logger.error("❌ 多次连接超时，程序将保存现有上下文并退出")
                raise
        except Exception as e:
            err_msg = str(e) or type(e).__name__
            logger.warning(f"⚠️  连接失败 ({err_msg})")
            if attempt < max_retries:
                # BLE MAC 可能已轮换, 重新发现
                logger.info("🔄 重新扫描设备...")
                new_mac, new_name = await auto_discover_mac()
                if new_mac:
                    device_mac, device_name = new_mac, new_name
                    collector = make_collector(device_mac, device_name)
                await asyncio.sleep(2)
            else:
                logger.error("❌ 多次重试后仍无法连接设备!")
                logger.info("")
                logger.info("💡 排查建议：")
                logger.info("   1. 确认头环已开机（指示灯亮起）")
                logger.info("   2. 确认头环在蓝牙范围内（< 10 米）")
                logger.info("   3. 尝试关闭再重新打开头环电源")
                sys.exit(1)

    # ---- 6. 主循环 — UNO Q 蓝牙控制器不支持可靠的热恢复 ----
    # A real BLE disconnect can make the controller disappear from BlueZ on
    # this board. Exit safely and preserve the recording instead of entering an
    # endless scan/cloud-reconnect loop. Restarting the board is then explicit.
    try:
        await disconnect_event.wait()
        await publish_status(ble_connected=False, reconnecting=False, data_stale=True)
        if stop_reason == "eeg_timeout":
            logger.error("❌ EEG 数据流长时间停滞，程序将安全保存并退出")
        else:
            logger.error("❌ Flowtime 蓝牙连接已断开，程序将安全保存并退出")
        logger.error("   若 bluetoothctl show 提示无控制器，请重启 UNO Q 后再运行")
    except KeyboardInterrupt:
        print("\n")
        logger.info("⏹️  用户中断")
    finally:
        # ---- 7. 清理 ----
        shutting_down = True
        logger.info(f"📊 {session.summary()}")

        if ble_fault_exit:
            # Avoid STOP_ALL / GATT cleanup after a silent or broken link. On
            # UNO Q that cleanup has produced ATT 0x0e and then removed the
            # HCI controller from BlueZ. Process exit releases the D-Bus side.
            logger.info("🛡️ BLE 故障退出：跳过 GATT 停止写入以保护蓝牙控制器")
        else:
            try:
                await collector.stop()
                await collector.wait_for_stop()
            except Exception as exc:
                logger.debug(f"BLE 清理时忽略错误: {exc}")

        # 按 SDK 生命周期先结束情感服务，再关闭云端会话与 WebSocket。
        try:
            # Set this before closing the socket. The SDK otherwise sees our
            # intentional close as an error and starts a five-second reconnect.
            client.closed = True
            if session.session_created and client.ws is not None:
                if session.affective_ready:
                    await client.finish_affective_service(services=AFFECTIVE_SERVICE_LIST)
                    await asyncio.sleep(0.2)
                await client.close_session()
                await asyncio.sleep(0.2)
            if client.ws is not None:
                await client.ws.close()
        except Exception as exc:
            logger.warning(f"⚠️ 云端会话清理未完全完成: {exc}")

        session_writer.close()
        _session_writer_for_ws = None

        # 取消所有后台任务 (WebSocket server, 广播等)
        for task in list(_background_tasks):
            task.cancel()
        if _background_tasks:
            await asyncio.gather(*_background_tasks, return_exceptions=True)

        logger.info("👋 已退出")


# ============================================================================
# 入口
# ============================================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 已退出")
