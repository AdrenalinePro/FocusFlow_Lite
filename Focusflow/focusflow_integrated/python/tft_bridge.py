"""MPU → MCU TFT command forwarder (Router Bridge).

Forwards JSON-over-Bridge commands from the Linux side of the UNO Q to
the STM32 sketch that drives the ILI9341V TFT.  The wire format is
exactly the JSON dialect that ``source_code/TFT_UI/focusflow_demo.ino``
parsed over USB serial — see ``sketch/sketch.ino`` for the sketch side.

Public surface (all sync, thread-safe):

* :meth:`TFTBridge.show_focus` — render the focus / studying screen.
* :meth:`TFTBridge.show_alert` — render the distraction-alert screen.
* :meth:`TFTBridge.show_break` — render the break / rest screen.
* :meth:`TFTBridge.ping`        — ask the MCU to refresh its health state.
* :meth:`TFTBridge.last_status` — last known TFT health string
  (``running`` / ``error`` / ``offline`` / ``unknown``).

The MCU sketch pushes ``tft_heartbeat`` every ``TFT_HEARTBEAT_MS``
milliseconds and answers ``tft_status`` Bridge.call queries.  Both are
delivered to ``Bridge.provide`` callbacks on the Python side and merged
into ``last_status`` here.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

# ``Bridge`` is provided by the Arduino App framework at runtime; the
# import is wrapped so unit tests on a workstation (where the module
# is not installed) can still import this file.
try:  # pragma: no cover - exercised on the UNO Q only
    from arduino.app_utils import Bridge  # type: ignore
except ImportError:  # pragma: no cover - developer workstation
    Bridge = None  # type: ignore

LOGGER = logging.getLogger("focusflow.tft")


# ── Defaults used when callers omit a field ──────────────────────────
# Mirrors the constants in focusflow_demo.ino.
DEFAULT_PCT = 82
DEFAULT_ELAPSED = 1122
DEFAULT_TOTAL = 1500
DEFAULT_SCREEN = "VS Code"
DEFAULT_STATUS = "高度专注"
DEFAULT_ALERT = "B站"
DEFAULT_REMAIN = 154
DEFAULT_NEXT = 1500


class TFTBridge:
    """Synchronous helper that wraps ``Bridge.notify`` / ``Bridge.provide``."""

    BRIDGE_CMD_NAME = "tft_cmd"        # Python → MCU
    BRIDGE_PING_NAME = "tft_status"    # Python → MCU, used as Bridge.call
    BRIDGE_HEARTBEAT_NAME = "tft_heartbeat"  # MCU → Python (notify)
    BRIDGE_HEALTH_NAME = "tft_status"  # MCU → Python (notify alias)

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or LOGGER
        self._lock = threading.Lock()
        self._last_status = "unknown"
        self._last_heartbeat_at: Optional[float] = None

        if Bridge is not None:
            try:
                Bridge.provide(
                    self.BRIDGE_HEARTBEAT_NAME,
                    self._on_mcu_heartbeat,
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.warning("Bridge.provide(%s) failed: %s",
                                     self.BRIDGE_HEARTBEAT_NAME, exc)

    # ── render helpers (Python → MCU) ───────────────────────────────
    def show_focus(
        self,
        *,
        pct: int = DEFAULT_PCT,
        elapsed: int = DEFAULT_ELAPSED,
        total: int = DEFAULT_TOTAL,
        screen: str = DEFAULT_SCREEN,
        status: str = DEFAULT_STATUS,
    ) -> bool:
        return self._send_json({
            "cmd": "focus",
            "pct": int(pct),
            "elapsed": int(elapsed),
            "total": int(total),
            "screen": screen,
            "status": status,
        })

    def show_alert(self, screen: str = DEFAULT_ALERT) -> bool:
        return self._send_json({"cmd": "alert", "screen": screen})

    def show_break(
        self,
        remain: int = DEFAULT_REMAIN,
        next_sess: int = DEFAULT_NEXT,
    ) -> bool:
        return self._send_json({
            "cmd": "break",
            "remain": int(remain),
            "next": int(next_sess),
        })

    def ping(self) -> bool:
        """Fire a ``ping`` JSON; the MCU re-renders the focus screen.

        We piggy-back on the focus render as the liveness probe instead
        of inventing a new verb — the sketch already treats ``ping`` as
        a no-op for the wire and uses it as a health check.
        """

        return self._send_json({"cmd": "ping"})

    # ── health reporting (MCU → Python) ─────────────────────────────
    def last_status(self) -> str:
        with self._lock:
            return self._last_status

    def seconds_since_heartbeat(self) -> Optional[float]:
        with self._lock:
            return (
                None if self._last_heartbeat_at is None
                else self._monotonic() - self._last_heartbeat_at
            )

    # ── internals ───────────────────────────────────────────────────
    def _send_json(self, payload: Dict[str, Any]) -> bool:
        if Bridge is None:
            self._logger.debug("Bridge unavailable; dropping %s", payload)
            return False
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            self._logger.warning("cannot encode TFT payload %r: %s", payload, exc)
            return False
        try:
            # ``Bridge.notify`` is fire-and-forget: the TFT render takes
            # ~200 ms on the MCU and the Windows client never needs the
            # result, so blocking the call would only add latency.
            Bridge.notify(self.BRIDGE_CMD_NAME, text)
            return True
        except Exception as exc:
            self._logger.warning("Bridge.notify(%s) failed: %s",
                                 self.BRIDGE_CMD_NAME, exc)
            return False

    def _on_mcu_heartbeat(self, status: Optional[str] = None) -> None:
        # ``Bridge.provide`` callbacks sometimes receive the value
        # directly and sometimes wrapped in a list/tuple depending on
        # how many parameters the MCU side sent.  Normalise here.
        if isinstance(status, (list, tuple)):
            status = status[0] if status else None
        if not isinstance(status, str):
            status = "running"
        with self._lock:
            self._last_status = status
            self._last_heartbeat_at = self._monotonic()
        self._logger.debug("TFT heartbeat: %s", status)

    @staticmethod
    def _monotonic() -> float:
        import time
        return time.monotonic()
