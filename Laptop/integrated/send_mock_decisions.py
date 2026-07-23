#!/usr/bin/env python3
"""Standalone UNO Q serial sender — simulates FocusFlow decision_update traffic.

Drop anywhere, no project imports.  Requires only pyserial::

    pip install pyserial

Usage::

    python send_mock_decisions.py COM9
    python send_mock_decisions.py COM9 --interval 1.0
    python send_mock_decisions.py COM9 --duration 60
    python send_mock_decisions.py COM9 --no-random  (fixed focused state)
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from typing import Any, Dict, List

try:
    import serial
except ImportError:
    sys.exit("缺少 pyserial。请先安装: pip install pyserial")

# ── protocol constants (mirrors FocusFlow v1.0 wire format) ──────────────

MAX_JSON_BYTES = 240
BAUDRATE = 115200

STATES: List[Dict[str, Any]] = [
    {"state": "focused",         "score": 85, "signal_ok": True,  "app": "VSCode"},
    {"state": "focused",         "score": 92, "signal_ok": True,  "app": "PyCharm"},
    {"state": "focused",         "score": 78, "signal_ok": True,  "app": "Terminal"},
    {"state": "distracted",      "score": 45, "signal_ok": True,  "app": "Browser"},
    {"state": "distracted",      "score": 38, "signal_ok": True,  "app": "WeChat"},
    {"state": "procrastinating", "score": 20, "signal_ok": True,  "app": "Bilibili"},
    {"state": "procrastinating", "score": 15, "signal_ok": True,  "app": "Douyin"},
    {"state": "resting",         "score": None, "signal_ok": False, "app": ""},
    {"state": "waiting",         "score": None, "signal_ok": False, "app": ""},
]


def build_message(seq: int, data: dict) -> bytes:
    """Encode one FocusFlow decision_update frame."""
    ts = int(time.time())
    msg = {"type": "decision_update", "seq": seq, "ts": ts, "data": data}
    payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("payload too large: %d bytes" % len(payload))
    return payload + b"\n"


def pick_random_state() -> dict:
    """Return a weighted-random decision payload.

    Weights are biased so ``focused`` appears most often, matching real
    usage where the user is mostly working.
    """
    weights = [30, 15, 10, 10, 5, 5, 5, 3, 2]
    template = random.choices(STATES, weights=weights, k=1)[0]
    data = dict(template)
    # Add small jitter to scores so the UNO Q sees variation.
    if data["score"] is not None:
        data["score"] = max(0, min(100, data["score"] + random.randint(-5, 5)))
    data["duration"] = round(random.uniform(0.5, 30.0), 1)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FocusFlow mock serial sender — 向 UNO Q 发送模拟决策数据",
    )
    parser.add_argument("port", help="串口号，如 COM9")
    parser.add_argument("--baud", type=int, default=BAUDRATE,
                        help="波特率，默认 115200")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="发送间隔秒数，默认 1.0")
    parser.add_argument("--duration", type=float, default=0,
                        help="运行秒数；0 表示持续运行直到 Ctrl+C")
    parser.add_argument("--no-random", action="store_true",
                        help="固定发送 focused 状态，不随机切换")
    args = parser.parse_args()

    print(f"打开 {args.port} @ {args.baud} baud ...")
    try:
        port = serial.Serial(args.port, baudrate=args.baud, timeout=0.2,
                             write_timeout=0.2)
    except (OSError, serial.SerialException) as exc:
        sys.exit(f"无法打开串口 {args.port}: {exc}")

    port.reset_input_buffer()
    print(f"串口 {args.port} 已打开。发送间隔 {args.interval}s，按 Ctrl+C 停止。\n")

    seq = 0
    started = time.monotonic()
    try:
        while True:
            if args.no_random:
                data = {"state": "focused", "score": 87, "duration": 5.0,
                        "signal_ok": True, "app": "VSCode"}
            else:
                data = pick_random_state()

            frame = build_message(seq, data)
            port.write(frame)
            port.flush()

            elapsed = time.monotonic() - started
            print(f"[{elapsed:6.1f}s]  seq={seq:<5}  state={data['state']:<16}  "
                  f"score={str(data['score']):>4s}  app={data.get('app', '')}")

            seq = (seq + 1) % 2**32

            if args.duration > 0 and elapsed >= args.duration:
                print(f"\n已运行 {args.duration}s，正常退出。共发送 {seq} 条。")
                break

            time.sleep(args.interval)
    except KeyboardInterrupt:
        elapsed = time.monotonic() - started
        print(f"\n\n已中断。运行 {elapsed:.1f}s，共发送 {seq} 条。")
    finally:
        port.close()
        print(f"串口 {args.port} 已关闭。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
