#!/usr/bin/env python3
"""Small, clock-independent rest-session state machine."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


@dataclass
class RestSession:
    started_at: float
    ends_at: float
    duration_seconds: int
    reason: str


class RestController:
    """Own the authoritative rest timer used by every laptop client."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._session: Optional[RestSession] = None

    @property
    def active(self) -> bool:
        return self._session is not None and self._clock() < self._session.ends_at

    def start(self, duration_seconds: int, reason: str = "manual") -> dict:
        duration = int(duration_seconds)
        if not 1 <= duration <= 24 * 60 * 60:
            raise ValueError("duration_seconds must be between 1 and 86400")
        now = self._clock()
        self._session = RestSession(
            started_at=now,
            ends_at=now + duration,
            duration_seconds=duration,
            reason=str(reason or "manual")[:32],
        )
        return self.snapshot()

    def extend(self, duration_seconds: int) -> dict:
        duration = int(duration_seconds)
        if duration < 1:
            raise ValueError("duration_seconds must be positive")
        if not self.active or self._session is None:
            raise ValueError("cannot extend when not resting")
        self._session.ends_at += duration
        self._session.duration_seconds += duration
        return self.snapshot()

    def stop(self) -> dict:
        previous = self.snapshot()
        self._session = None
        return {
            **previous,
            "active": False,
            "remaining_seconds": 0,
            "phase": "ended",
            "outputs_paused": False,
        }

    def poll(self) -> Tuple[dict, bool]:
        """Return the current snapshot and whether rest ended on this poll."""
        if self._session is None:
            return self.snapshot(), False
        if self._clock() < self._session.ends_at:
            return self.snapshot(), False
        ended = self.stop()
        return ended, True

    def snapshot(self) -> dict:
        if self._session is None:
            return {
                "type": "rest_state",
                "active": False,
                "remaining_seconds": 0,
                "duration_seconds": 0,
                "reason": None,
                "phase": "idle",
                "outputs_paused": False,
            }
        remaining = max(0, int(self._session.ends_at - self._clock() + 0.999))
        if remaining <= 30:
            phase = "ending"
        elif remaining > self._session.duration_seconds * 0.8:
            phase = "start"
        else:
            phase = "middle"
        return {
            "type": "rest_state",
            "active": remaining > 0,
            "remaining_seconds": remaining,
            "duration_seconds": self._session.duration_seconds,
            "reason": self._session.reason,
            "phase": phase,
            "outputs_paused": remaining > 0,
        }
