#!/usr/bin/env python3
"""FocusFlow UNO Q serial receiver — single-file, zero-dependency.

Drop anywhere on the UNO Q and run::

    python3 focusflow_serial.py --duration 60
    python3 focusflow_serial.py --duration 0    # run until Ctrl+C

Protocol: newline-delimited compact JSON over ``/dev/ttyGS0`` at 115200 baud.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import termios
import time
from typing import Any, Dict, Optional

# ======================================================================
# protocol constants (v1.0 wire format)
# ======================================================================

MAX_JSON_BYTES = 240
UINT32_MAX = 2**32 - 1
STATES = {"focused", "distracted", "procrastinating", "resting"}
SCREEN_STATES = {"focused", "distracted", "procrastinating", "away"}
REST_ACTIONS = {"start", "stop", "extend", "query"}
FEEDBACK_TYPES = {"none", "vibrate_short", "vibrate_double", "vibrate_continuous",
                  "notification", "tft_alert"}

UPLINK_TYPES = {"eye_data", "screen_data", "rest_command", "heartbeat", "sync_request"}
DOWNLINK_TYPES = {"state_update", "focus_score", "rest_countdown", "display_content",
                  "device_status", "vibration_feedback", "heartbeat", "sync_response", "error"}

BAUD_MAP = {
    9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
    57600: termios.B57600, 115200: termios.B115200, 230400: termios.B230400,
    460800: termios.B460800, 921600: termios.B921600,
}


# ======================================================================
# message encode / decode
# ======================================================================

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check(cond: bool, msg: str, code: str = "INVALID_JSON") -> None:
    if not cond:
        raise ValueError("%s: %s" % (code, msg))


def _validate(msg_type: str, data: Dict[str, Any]) -> None:
    _check(isinstance(data, dict), "data must be an object")
    if msg_type == "eye_data":
        for f in ("yaw", "pitch", "state_duration", "confidence"):
            _check(f in data, "missing data.%s" % f, "MISSING_FIELD")
            _check(_is_number(data[f]), "%s must be number" % f)
        _check(-180 <= data["yaw"] <= 180, "yaw out of range", "OUT_OF_RANGE")
        _check(-90 <= data["pitch"] <= 90, "pitch out of range", "OUT_OF_RANGE")
        _check(0 <= data["confidence"] <= 1, "confidence out of range", "OUT_OF_RANGE")
        _check("is_focused" in data, "missing data.is_focused", "MISSING_FIELD")
        _check(isinstance(data["is_focused"], int), "is_focused must be int")
        _check(data["is_focused"] in (0, 1), "is_focused out of range", "OUT_OF_RANGE")
    elif msg_type == "screen_data":
        _check("state" in data, "missing data.state", "MISSING_FIELD")
        _check(data["state"] in SCREEN_STATES, "invalid screen state", "OUT_OF_RANGE")
        _check("confidence" in data, "missing data.confidence", "MISSING_FIELD")
        _check(_is_number(data["confidence"]), "confidence must be number")
    elif msg_type == "rest_command":
        _check("action" in data, "missing data.action", "MISSING_FIELD")
        _check(data["action"] in REST_ACTIONS, "invalid rest action", "OUT_OF_RANGE")
    elif msg_type == "heartbeat":
        _check("uptime" in data, "missing data.uptime", "MISSING_FIELD")
    elif msg_type == "sync_request":
        pass
    else:
        _check(False, "unknown message type: %s" % msg_type, "INVALID_MSG_TYPE")


def encode(msg_type: str, data: Dict[str, Any], seq: int,
           ts: Optional[int] = None) -> bytes:
    if ts is None:
        ts = int(time.time())
    msg = {"type": msg_type, "seq": seq, "ts": ts, "data": dict(data)}
    _check(msg_type in (UPLINK_TYPES | DOWNLINK_TYPES),
           "unknown message type: %s" % msg_type, "INVALID_MSG_TYPE")
    _check(0 <= seq <= UINT32_MAX, "seq out of range", "OUT_OF_RANGE")
    _check(0 <= ts <= UINT32_MAX, "ts out of range", "OUT_OF_RANGE")
    _validate(msg_type, data)
    payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("OUT_OF_RANGE: %d bytes > %d" % (len(payload), MAX_JSON_BYTES))
    return payload


def decode(payload: bytes) -> dict:
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("OUT_OF_RANGE: %d bytes" % len(payload))
    msg = json.loads(payload.decode("utf-8"))
    t, s, ts, d = msg["type"], msg["seq"], msg["ts"], msg.get("data", {})
    _validate(t, d)
    return {"type": t, "seq": s, "ts": ts, "data": d}


# ======================================================================
# serial transport — your working termios pattern
# ======================================================================

class SerialPort:
    def __init__(self, port: str, baudrate: int = 115200) -> None:
        baud_const = BAUD_MAP.get(baudrate)
        if baud_const is None:
            raise ValueError("unsupported baud: %d" % baudrate)
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self._fd)
        attrs[4] = baud_const   # ispeed
        attrs[5] = baud_const   # ospeed
        attrs[2] = attrs[2] & ~termios.CSIZE | termios.CS8
        attrs[2] = attrs[2] & ~termios.PARENB
        attrs[2] = attrs[2] & ~termios.CSTOPB
        attrs[2] = attrs[2] & ~termios.CRTSCTS
        termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        self._buf = bytearray()
        self._closed = False

    async def write(self, data: bytes) -> None:
        if not self._closed:
            await asyncio.to_thread(os.write, self._fd, data)

    async def read_line(self) -> bytes:
        while True:
            if self._closed:
                raise EOFError("serial closed")
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                return line
            try:
                chunk = await asyncio.to_thread(os.read, self._fd, 4096)
            except BlockingIOError:
                chunk = b""
            except OSError:
                chunk = b""
            if chunk:
                self._buf.extend(chunk)
            else:
                await asyncio.sleep(0.05)

    def close(self) -> None:
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass


# ======================================================================
# state machine
# ======================================================================

class StateMachine:
    def __init__(self) -> None:
        self.current_state = "focused"
        self.focus_score = 85
        self.prev_state = "focused"
        self.last_eye: Optional[dict] = None
        self.last_screen: Optional[dict] = None
        self._state_fb = {
            "focused": "none", "distracted": "vibrate_short",
            "procrastinating": "vibrate_double", "resting": "vibrate_continuous",
        }

    def update(self, eye=None, screen=None) -> None:
        if eye is not None:
            self.last_eye = dict(eye)
        if screen is not None:
            self.last_screen = dict(screen)

    def decide(self) -> tuple:
        score = self._score()
        ss = (self.last_screen or {}).get("state")
        prev = self.current_state
        if prev == "resting":
            new_state = "resting"
        elif ss == "procrastinating":
            new_state = "procrastinating"
        elif score < 30:
            new_state = "distracted"
        elif ss == "away":
            new_state = "distracted"
        else:
            new_state = "focused"
        fb = self._state_fb.get(new_state, "none")
        return new_state, score, prev, fb

    def commit(self, new_state: str, score: int, prev: str) -> bool:
        self.focus_score = score
        if new_state == self.current_state:
            return False
        self.prev_state = prev if prev in STATES else self.current_state
        self.current_state = new_state
        return True

    def _score(self) -> int:
        if not self.last_eye:
            return self.focus_score
        conf = float(self.last_eye.get("confidence", 0.5) or 0.0)
        focused = int(self.last_eye.get("is_focused", 0) or 0)
        base = max(0, min(100, int(round(conf * 100))))
        return min(base, 40) if not focused else base


# ======================================================================
# sequence dedup
# ======================================================================

class SeqTracker:
    HALF = 2**31

    def __init__(self) -> None:
        self._last_seq: Optional[int] = None
        self._last_ts: Optional[int] = None

    def accept(self, seq: int, ts: int) -> bool:
        if self._last_seq is None:
            self._last_seq, self._last_ts = seq, ts
            return True
        dist = (seq - self._last_seq) % 2**32
        if 0 < dist < self.HALF:
            self._last_seq, self._last_ts = seq, ts
            return True
        if dist != 0 and self._last_ts is not None and ts > self._last_ts and dist > self.HALF:
            self._last_seq, self._last_ts = seq, ts
            return True
        return False


# ======================================================================
# main service
# ======================================================================

class FocusFlowSerial:
    def __init__(self, port: str, baud: int = 115200, background: bool = True):
        self._port = port
        self._baud = baud
        self._background = background
        self._serial: Optional[SerialPort] = None
        self._seq = 0
        self._seq_tracker = SeqTracker()
        self._driver = StateMachine()
        self._lock: Optional[asyncio.Lock] = None
        self._stop = False
        self._started_at = 0.0
        self._rest_start: Optional[float] = None
        self._rest_dur = 0
        self.rx_count = 0
        self.tx_count = 0
        self.errors = 0

    def _next_seq(self) -> int:
        v = self._seq
        self._seq = (self._seq + 1) % 2**32
        return v

    async def send(self, msg_type: str, data: dict) -> bool:
        if self._serial is None:
            return False
        payload = encode(msg_type, data, self._next_seq())
        async with self._lock:
            await self._serial.write(payload + b"\n")
        self.tx_count += 1
        print("[TX] %s  seq=%s  %s" % (msg_type, self._seq - 1, json.dumps(data, ensure_ascii=False)))
        return True

    # ---- message handlers ----

    async def _on_eye(self, data: dict) -> None:
        self._driver.update(eye=data)

    async def _on_screen(self, data: dict) -> None:
        self._driver.update(screen=data)

    async def _on_rest(self, data: dict) -> None:
        action = data.get("action", "")
        if action == "start":
            self._rest_start = time.time()
            self._rest_dur = int(data.get("duration", 300))
            self._driver.current_state = "resting"
            print("[SERIAL] rest started  duration=%ds" % self._rest_dur)
        elif action == "stop":
            self._rest_start = None
            if self._driver.current_state == "resting":
                self._driver.current_state = "focused"
            print("[SERIAL] rest stopped")
        elif action == "extend":
            if self._rest_start is not None:
                self._rest_dur += int(data.get("duration", 60))
        elif action == "query":
            if self._rest_start is not None:
                rem = max(0, int(self._rest_start + self._rest_dur - time.time()))
                await self.send("rest_countdown", {
                    "remaining": rem, "total": self._rest_dur,
                    "state": "resting", "phase": "middle",
                })

    async def _on_heartbeat(self, msg: dict) -> None:
        await self.send("heartbeat", {
            "uptime": int(time.time() - self._started_at),
            "echo_seq": msg["seq"],
        })

    async def _on_sync(self, data: dict) -> None:
        rest = None
        if self._rest_start is not None:
            rem = max(0, int(self._rest_start + self._rest_dur - time.time()))
            rest = {"remaining": rem, "total": self._rest_dur,
                    "state": "resting", "phase": "middle"}
        await self.send("sync_response", {
            "state": self._driver.current_state,
            "focus_score": self._driver.focus_score,
            "prev_state": self._driver.prev_state,
            "rest_countdown": rest,
            "device_status": {"tft_display": "running"},
        })

    # ---- background loops ----

    async def _focus_loop(self) -> None:
        while not self._stop:
            new_state, score, prev, fb = self._driver.decide()
            if self._driver.commit(new_state, score, prev):
                await self.send("state_update", {
                    "state": new_state, "focus_score": score,
                    "prev_state": prev, "duration_in_state": 0,
                    "triggered_feedback": fb,
                })
            else:
                await self.send("focus_score", {
                    "score": score, "state": self._driver.current_state,
                })
            await asyncio.sleep(1.0)

    async def _rest_timer(self) -> None:
        while not self._stop:
            if self._rest_start is not None:
                rem = max(0, int(self._rest_start + self._rest_dur - time.time()))
                if rem == 0:
                    self._rest_start = None
                    if self._driver.current_state == "resting":
                        self._driver.current_state = "focused"
                    print("[SERIAL] rest timer expired")
                else:
                    await self.send("rest_countdown", {
                        "remaining": rem, "total": self._rest_dur,
                        "state": "resting",
                        "phase": "ending" if rem < 30 else "middle",
                    })
            await asyncio.sleep(10.0)

    # ---- main ----

    async def run(self) -> None:
        self._serial = SerialPort(self._port, self._baud)
        self._lock = asyncio.Lock()
        self._started_at = time.time()
        print("[SERIAL] opened %s @ %d baud  等待 Windows 端数据..." % (self._port, self._baud))
        tasks = []
        if self._background:
            tasks.append(asyncio.create_task(self._focus_loop()))
            tasks.append(asyncio.create_task(self._rest_timer()))
        try:
            while not self._stop:
                try:
                    raw = await self._serial.read_line()
                except EOFError:
                    print("[SERIAL] port closed")
                    break
                if not raw:
                    continue
                self.rx_count += 1
                try:
                    msg = decode(raw)
                except (ValueError, json.JSONDecodeError) as e:
                    self.errors += 1
                    print("[SERIAL] decode error: %s  raw=%r" % (e, raw[:80]))
                    continue
                if not self._seq_tracker.accept(msg["seq"], msg["ts"]):
                    continue
                print("[RX] %s  seq=%s  %s" % (msg["type"], msg["seq"],
                      json.dumps(msg["data"], ensure_ascii=False)))
                t = msg["type"]
                if t == "eye_data":
                    await self._on_eye(msg["data"])
                elif t == "screen_data":
                    await self._on_screen(msg["data"])
                elif t == "rest_command":
                    await self._on_rest(msg["data"])
                elif t == "heartbeat":
                    await self._on_heartbeat(msg)
                elif t == "sync_request":
                    await self._on_sync(msg["data"])
        finally:
            self._stop = True
            for t in tasks:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
            self._serial.close()
            print("[SERIAL] stopped — rx=%d  tx=%d  errors=%d" % (
                self.rx_count, self.tx_count, self.errors))


# ======================================================================
# CLI
# ======================================================================

def main() -> int:
    p = argparse.ArgumentParser(description="FocusFlow UNO Q serial receiver")
    p.add_argument("--port", default="/dev/ttyGS0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--duration", type=float, default=60,
                   help="seconds to run (0 = forever)")
    p.add_argument("--no-background", action="store_true",
                   help="disable automatic focus_score / rest timer")
    args = p.parse_args()

    if args.baud not in BAUD_MAP:
        print("unsupported baud rate: %d" % args.baud)
        print("supported: %s" % sorted(BAUD_MAP.keys()))
        return 1

    svc = FocusFlowSerial(args.port, args.baud, background=not args.no_background)

    async def _run() -> int:
        runner = asyncio.create_task(svc.run())
        try:
            if args.duration > 0:
                await asyncio.sleep(args.duration)
            else:
                while not svc._stop:
                    await asyncio.sleep(1)
        except KeyboardInterrupt:
            print()
        finally:
            svc._stop = True
            await runner
        return 0 if svc.errors == 0 else 1

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
