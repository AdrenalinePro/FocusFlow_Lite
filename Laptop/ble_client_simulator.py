#!/usr/bin/env python3
"""
FocusFlow Lite — 笔记本端 BLE Client 模拟器
===========================================

**运行在笔记本上** (不是 UNO Q). 用来和真机 UNO Q 做端到端联调.

工作内容:
  1. 扫描名为 UNO-Q-FF01 的设备
  2. 连接, 请求 MTU=512, 订阅 UNO_TX / STATE_SYNC 的 Notify
  3. 启动 1 Hz 心跳循环
  4. 启动 1 Hz 模拟传感器数据写入循环 (随机专注/走神)
  5. 用户按 Ctrl+C 模拟一次走神事件
  6. 收到 UNO Q 的 feedback_cmd 时打印

依赖:
  pip install bleak

跑法:
  python3 ble_client_simulator.py --name UNO-Q-FF01
  python3 ble_client_simulator.py --address AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
import time
from typing import Optional

logger = logging.getLogger("ble.client")


# 协议常量 — 必须和 UNO Q 端 ble_transport.py 一致
SERVICE_UUID    = "0000FF00-0000-1000-8000-00805F9B34FB"
CHAR_LAPTOP_TX  = "0000FF01-0000-1000-8000-00805F9B34FB"
CHAR_UNO_TX     = "0000FF02-0000-1000-8000-00805F9B34FB"
CHAR_STATE_SYNC = "0000FF03-0000-1000-8000-00805F9B34FB"
CHAR_HEARTBEAT  = "0000FF04-0000-1000-8000-00805F9B34FB"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def find_device(name: Optional[str], address: Optional[str]):
    """扫描并找到目标 UNO Q 设备."""
    from bleak import BleakScanner

    if address:
        logger.info("直接通过地址连接: %s", address)
        return address

    logger.info("扫描设备 (%s)...", name)
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        if d.name == name:
            logger.info("找到 %s -> %s", d.name, d.address)
            return d.address

    found = [d for d in devices if d.name]
    logger.error("没找到名字为 %s 的设备, 当前能扫到的有: %s",
                 name, [(d.name, d.address) for d in found[:5]])
    raise SystemExit(1)


class Simulator:
    """一个用 bleak 实现的最小客户端."""

    def __init__(self, address: str):
        self.address = address
        self.client = None
        self.heartbeat_counter = 0
        self._send_event = asyncio.Event()
        self._stop = asyncio.Event()
        self.last_feedback: Optional[dict] = None

    async def connect_and_setup(self):
        from bleak import BleakClient

        self.client = BleakClient(self.address, timeout=20.0)
        await self.client.connect()
        logger.info("✅ 已连接: %s", self.address)

        # 请求 MTU
        try:
            await self.client.request_mtu(512)
            logger.info("MTU 协商: %d", self.client.mtu_size)
        except Exception as e:
            logger.warning("MTU 协商失败: %s", e)

        # 订阅 UNO_TX Notify (收 feedback_cmd)
        await self.client.start_notify(
            CHAR_UNO_TX, self._on_uno_tx_notify,
        )
        logger.info("已订阅 UNO_TX Notify")

        # 订阅 STATE_SYNC Notify
        await self.client.start_notify(
            CHAR_STATE_SYNC, self._on_state_sync_notify,
        )
        logger.info("已订阅 STATE_SYNC Notify")

    def _on_uno_tx_notify(self, sender, data: bytearray):
        try:
            packet = json.loads(data.decode("utf-8"))
        except Exception as e:
            logger.error("UNO_TX 解析失败: %s", e)
            return
        logger.warning("📥 收到 feedback_cmd: %s",
                       packet.get("payload", {}).get("notify_msg", "?"))
        self.last_feedback = packet

    def _on_state_sync_notify(self, sender, data: bytearray):
        try:
            packet = json.loads(data.decode("utf-8"))
        except Exception as e:
            logger.error("STATE_SYNC 解析失败: %s", e)
            return
        logger.info("📥 UNO Q 状态变更: %s",
                    packet.get("payload", {}).get("system_state", "?"))

    async def write_laptop_tx(self, packet: dict):
        raw = json.dumps(packet, ensure_ascii=False).encode("utf-8")
        await self.client.write_gatt_char(CHAR_LAPTOP_TX, raw, response=False)
        logger.debug("→ LAPTOP_TX: %s", packet.get("type"))

    async def write_state_sync(self, state: str, sub: str = ""):
        raw = json.dumps({
            "type": "state_sync",
            "payload": {"system_state": state, "sub_state": sub},
        }, ensure_ascii=False).encode("utf-8")
        await self.client.write_gatt_char(CHAR_STATE_SYNC, raw, response=False)
        logger.info("→ STATE_SYNC: %s/%s", state, sub)

    async def write_heartbeat(self):
        self.heartbeat_counter = (self.heartbeat_counter + 1) % 256
        await self.client.write_gatt_char(
            CHAR_HEARTBEAT, bytes([self.heartbeat_counter]), response=False,
        )

    async def write_distraction_event(self, severity: str, app: str):
        await self.write_laptop_tx({
            "type": "distraction_event",
            "payload": {
                "event_type": "slacking",
                "severity": severity,
                "source": "screen",
                "details": {
                    "app": app,
                    "reason": f"检测到 {app} 摸鱼",
                    "duration_sec": random.uniform(5, 60),
                },
            },
        })

    # ---- 三个常驻任务 ----
    async def heartbeat_loop(self):
        """每 1 秒写一次心跳."""
        while not self._stop.is_set():
            try:
                await self.write_heartbeat()
            except Exception as e:
                logger.error("心跳写入失败: %s", e)
                return
            await asyncio.sleep(1.0)

    async def sensor_loop(self):
        """每 1 秒模拟一次传感器数据."""
        apps = [("VSCode", 3), ("Chrome", 2), ("Bilibili", 1),
                ("WeChat", 1), ("Terminal", 3)]
        while not self._stop.is_set():
            try:
                is_focused = random.random() > 0.3
                app, cat = random.choice(apps)
                packet = {
                    "type": "sensor_data",
                    "payload": {
                        "eye": {
                            "yaw": random.uniform(-10, 10),
                            "pitch": random.uniform(-5, 5),
                            "is_focused": 1 if is_focused else 0,
                            "state_duration": random.uniform(0, 30),
                            "confidence": random.uniform(0.7, 0.99),
                        },
                        "screen": {
                            "state_code": 1.0 if is_focused and cat == 3 else 0.6,
                            "confidence": random.uniform(0.7, 0.99),
                            "app_category": cat,
                            "state": "专注" if is_focused else "走神",
                            "app": app,
                        },
                        "combined": {
                            "overall_focus": random.uniform(0.3, 0.95),
                        },
                    },
                }
                await self.write_laptop_tx(packet)
            except Exception as e:
                logger.error("sensor 写入失败: %s", e)
                return
            await asyncio.sleep(1.0)

    async def manual_event_loop(self):
        """命令行控制: 按 d 触发 high 走神, 按 r 切 resting, 按 m 切 monitoring."""
        sys.stdout.write(
            "\n控制台命令:\n"
            "  d → 触发 high 走神事件\n"
            "  r → 切换到 resting\n"
            "  m → 切换到 monitoring\n"
            "  q → 退出\n> "
        )
        sys.stdout.flush()
        loop = asyncio.get_running_loop()

        def on_stdin():
            line = sys.stdin.readline().strip().lower()
            if line == "d":
                asyncio.run_coroutine_threadsafe(
                    self.write_distraction_event("high", "Bilibili"),
                    loop,
                )
            elif line == "r":
                asyncio.run_coroutine_threadsafe(
                    self.write_state_sync("resting", "short_rest"),
                    loop,
                )
            elif line == "m":
                asyncio.run_coroutine_threadsafe(
                    self.write_state_sync("monitoring", ""),
                    loop,
                )
            elif line == "q":
                self._stop.set()
            sys.stdout.write("> ")
            sys.stdout.flush()

        loop.add_reader(sys.stdin.fileno(), on_stdin)
        try:
            await self._stop.wait()
        finally:
            try:
                loop.remove_reader(sys.stdin.fileno())
            except Exception:
                pass

    async def run(self):
        await self.connect_and_setup()
        tasks = [
            asyncio.create_task(self.heartbeat_loop()),
            asyncio.create_task(self.sensor_loop()),
            asyncio.create_task(self.manual_event_loop()),
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.client.disconnect()
            logger.info("已断开")


def parse_args():
    p = argparse.ArgumentParser(description="FocusFlow Lite 笔记本端 Client 模拟器")
    p.add_argument("--name", default="UNO-Q-FF01",
                   help="UNO Q 设备名 (扫描过滤)")
    p.add_argument("--address", default=None,
                   help="UNO Q 蓝牙地址 (跳过扫描)")
    p.add_argument("--log", default="INFO")
    return p.parse_args()


def main():
    args = parse_args()
    setup_logging(args.log)
    try:
        address = asyncio.run(find_device(args.name, args.address))
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1

    sim = Simulator(address)

    def _sigint():
        sim._stop.set()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.add_signal_handler(signal.SIGINT, _sigint)
        loop.run_until_complete(sim.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
