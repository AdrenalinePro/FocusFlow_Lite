"""Default focus state-machine for the FocusFlow UNO Q Linux side.

The protocol (section 6) only mandates the four states ``focused``,
``distracted``, ``procrastinating`` and ``resting`` plus a small set of
allowed transitions.  The actual decision logic is an implementation
detail of the UNO Q, so this module provides a deliberately simple
reference driver that mirrors the example in §10.1 of the protocol:

* it remembers the latest eye_data / screen_data;
* it derives a focus score from the eye confidence;
* it picks a state from the eye / screen combination;
* it returns the triggered feedback (used by ``state_update``).

Embedding a different model (e.g. an ONNX runtime inference) only
requires plugging a custom object that satisfies :class:`StateDriver`
into :class:`LinuxBLEServer`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .linux_ble_protocol import STATES

LOGGER = logging.getLogger(__name__)


@dataclass
class StateDriver:
    """Minimal focus state machine.

    The driver is intentionally pure: it reads the last known inputs and
    returns the next state, score and feedback.  Side effects (sending
    ``state_update`` over BLE, toggling the wristband, updating the TFT)
    are the caller's responsibility.
    """

    current_state: str = "focused"
    focus_score: int = 85
    prev_state: str = "focused"
    duration_in_state: float = 0.0
    last_transition_at: float = field(default_factory=time.time)

    # Latest raw inputs from Windows.  These mirror the example in the
    # protocol doc and are updated by the server's message dispatch loop.
    last_eye: Optional[Dict[str, Any]] = None
    last_screen: Optional[Dict[str, Any]] = None

    # Mapping from feedback mode strings used in ``state_update`` to the
    # same enum used in ``vibration_feedback.mode``.  Kept here so the
    # state machine and the wristband layer stay in sync.
    _FEEDBACK_FOR_STATE: Dict[str, str] = field(default_factory=lambda: {
        "focused": "none",
        "distracted": "vibrate_short",
        "procrastinating": "vibrate_double",
        "resting": "vibrate_continuous",
    })

    def update_inputs(
        self,
        eye: Optional[Dict[str, Any]] = None,
        screen: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update the latest eye / screen payloads (called by the server)."""

        if eye is not None:
            self.last_eye = dict(eye)
        if screen is not None:
            self.last_screen = dict(screen)

    def focus_score_from_eye(self) -> int:
        """Derive a 0-100 score from the last eye payload."""

        if not self.last_eye:
            return self.focus_score
        confidence = float(self.last_eye.get("confidence", 0.5) or 0.0)
        is_focused = int(self.last_eye.get("is_focused", 0) or 0)
        # Linear blend: high confidence + focused = high score.
        base = max(0, min(100, int(round(confidence * 100))))
        if not is_focused:
            base = min(base, 40)
        return base

    def decide(self) -> Tuple[str, int, str, str]:
        """Return ``(new_state, score, prev_state, feedback)``.

        ``prev_state`` is the state we were in *before* the transition
        (or the current state when nothing changes).  ``feedback`` is
        the ``triggered_feedback`` value to use in ``state_update``.
        """

        score = self.focus_score_from_eye()
        screen_state = (self.last_screen or {}).get("state")
        prev = self.current_state

        if prev == "resting":
            new_state = "resting"
        elif screen_state == "procrastinating":
            new_state = "procrastinating"
        elif score < 30:
            new_state = "distracted"
        elif screen_state == "away":
            new_state = "distracted"
        else:
            new_state = "focused"

        feedback = self._FEEDBACK_FOR_STATE.get(new_state, "none")
        return new_state, score, prev, feedback

    def commit(self, new_state: str, score: int, prev: str) -> bool:
        """Apply the transition.  Returns True when the state actually changed."""

        now = time.time()
        self.duration_in_state = now - self.last_transition_at
        self.focus_score = score
        if new_state == self.current_state:
            return False
        self.prev_state = prev if prev in STATES else self.current_state
        self.current_state = new_state
        self.last_transition_at = now
        LOGGER.debug(
            "state %s -> %s (score=%s, feedback=%s)",
            self.prev_state, self.current_state, self.focus_score,
            self._FEEDBACK_FOR_STATE.get(new_state, "none"),
        )
        return True
