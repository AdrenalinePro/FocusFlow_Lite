"""FocusFlow-specific LinuxBLEServer subclass.

The base :class:`linux.linux_ble_server.LinuxBLEServer` already does the
hard work of BlueZ D-Bus plumbing, JSON encoding / decoding, the focus
state machine, periodic ``focus_score`` / ``device_status`` / heartbeat
loops and ``rest_command`` bookkeeping.

What it does NOT do:

* send anything to the wristband — the ``vibration_feedback`` message
  it pushes back to Windows is just an acknowledgement;
* drive the TFT — that JSON-over-USB-Serial logic lives in
  ``source_code/TFT_UI/focusflow_demo.ino``;
* report real ``wristband_connected`` / ``tft_display`` state in
  ``device_status`` — the base class hard-codes ``False`` / "running";
* understand the laptop-driven ``decision_update`` message described
  in ``UNO_Q_BLE_DECISION_PROTOCOL.md`` (which is not yet part of the
  upstream ``UPLINK_TYPES`` set).

This subclass plugs all four holes without modifying any file under
``source_code/``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from linux.linux_ble_protocol import BLEMessage, STATES
from linux.linux_ble_server import LinuxBLEServer
from tft_bridge import TFTBridge
from wristband_controller import WristbandController

LOGGER = logging.getLogger("focusflow.server")

# ── Vibration policy (user requirement #3) ───────────────────────────
#   distracted / procrastinating → 3× @ intensity 40 (走神 / 摸鱼)
#   rest start (via rest_command) → 1× @ intensity 40 (进入休息)
#   rest end   (via rest_command) → 2× @ intensity 40 (休息结束)
DEFAULT_VIBRATION_INTENSITY = 40
VIBRATION_REPEATS_DISTRACTED = 3
VIBRATION_REPEATS_PROCRASTINATING = 3
VIBRATION_REPEATS_REST_START = 1
VIBRATION_REPEATS_REST_END = 2

# ── decision_update state set (UNO_Q_BLE_DECISION_PROTOCOL.md) ──────
# The supplement adds ``waiting`` to the state vocabulary the laptop can
# publish.  ``resting`` is also reachable via decision_update (e.g. when
# the laptop detects the user stood up without sending rest_command),
# but the supplement is explicit that ``resting`` must immediately stop
# any in-progress wristband output — see _on_state_change.
DECISION_UPDATE_STATES = frozenset({
    "focused",
    "distracted",
    "procrastinating",
    "waiting",
    "resting",
})

# triggered_feedback → what gets pushed to Windows in state_update.
# ``resting`` and ``waiting`` must NOT be ``vibrate_continuous`` per the
# supplement (the old state machine did that for resting).
FEEDBACK_FOR_STATE: Dict[str, str] = {
    "focused": "none",
    "distracted": "vibrate_short",
    "procrastinating": "vibrate_double",
    "waiting": "none",
    "resting": "none",
}

APP_NAME_MAX_LEN = 24


class FocusFlowBLEServer(LinuxBLEServer):
    """LinuxBLEServer wired to the wristband, the TFT bridge and the
    laptop-published ``decision_update`` message.
    """

    def __init__(
        self,
        config,
        *,
        wristband: WristbandController,
        tft: TFTBridge,
        vibration_intensity: int = DEFAULT_VIBRATION_INTENSITY,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(config=config, logger=(logger or LOGGER))

        self._wristband = wristband
        self._tft = tft
        self._intensity = int(vibration_intensity)

        # Track the last committed state so we know when to fire a
        # vibration.  The driver's ``current_state`` is the authoritative
        # value; this is just a memo for our transition detector.
        self._last_state: str = self._driver.current_state

        # Wire our state-transition observer.  The base class invokes
        # ``add_message_handler`` callbacks AFTER it has dispatched the
        # message, so by the time we run the driver has already seen
        # the latest eye_data / screen_data.
        self.add_message_handler(self._on_message_after_dispatch)

    # ── RX bypass: parse decision_update before the strict validator ─
    # The shared protocol module in source_code/ does NOT yet know
    # about decision_update — adding it there would require touching
    # the upstream ``UPLINK_TYPES`` set and ``validate_data()``.  To
    # honour the "do not modify source_code/" constraint we hook
    # ``_handle_rx`` and try to decode decision_update ourselves,
    # falling back to the standard decoder for everything else.
    async def _handle_rx(self, payload: bytes) -> None:
        decision = self._try_parse_decision_update(payload)
        if decision is not None:
            if not self._incoming_sequences.accept(decision.seq, decision.ts):
                self.logger.debug(
                    "discard duplicate/out-of-order decision_update seq=%s",
                    decision.seq,
                )
                return
            await self._dispatch(decision)
            return
        # Standard path: validate against UPLINK_TYPES, then dispatch.
        await super()._handle_rx(payload)

    async def _dispatch(self, message: BLEMessage) -> None:
        if message.type == "decision_update":
            await self._handle_decision_update(message)
            self._emit("message", message)
            return
        await super()._dispatch(message)

    def _try_parse_decision_update(
        self, payload: bytes,
    ) -> Optional[BLEMessage]:
        """Light-weight parser for the laptop's ``decision_update``.

        Returns ``None`` when the payload is not a decision_update
        (caller falls back to the standard decoder) or when the payload
        is malformed.  Field validation follows the table in
        ``UNO_Q_BLE_DECISION_PROTOCOL.md``:

        ============= ==================================================
        field          constraint
        ============= ==================================================
        state          one of DECISION_UPDATE_STATES
        score          integer 0..100 OR null
        duration       number >= 0
        signal_ok      boolean
        app            string, <= APP_NAME_MAX_LEN characters
        ============= ==================================================
        """

        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("type") != "decision_update":
            return None
        try:
            seq = int(raw.get("seq", 0))
            ts = int(raw.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if seq < 0 or ts < 0:
            return None

        data = raw.get("data")
        if not isinstance(data, dict):
            return None

        state = data.get("state")
        if state not in DECISION_UPDATE_STATES:
            self._emit(
                "error",
                "INVALID_JSON: decision_update.state=%r not in %s"
                % (state, sorted(DECISION_UPDATE_STATES)),
            )
            return None

        score = data.get("score")
        if score is not None:
            try:
                score = int(score)
            except (TypeError, ValueError):
                return None
            if not (0 <= score <= 100):
                return None

        duration = data.get("duration")
        if duration is not None:
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                return None
            if duration < 0:
                return None

        signal_ok = data.get("signal_ok", True)
        if not isinstance(signal_ok, bool):
            return None

        app = data.get("app")
        if app is not None:
            if not isinstance(app, str):
                return None
            if len(app) > APP_NAME_MAX_LEN:
                app = app[:APP_NAME_MAX_LEN]

        # Re-pack only the fields we accept (defensive: drop unknown
        # keys to keep the BLEMessage clean).
        normalised: Dict[str, Any] = {"state": state, "signal_ok": signal_ok}
        if score is not None:
            normalised["score"] = score
        if duration is not None:
            normalised["duration"] = duration
        if app is not None:
            normalised["app"] = app

        return BLEMessage(type="decision_update", seq=seq, ts=ts, data=normalised)

    # ── state-machine integration (eye_data / screen_data / heartbeat) ─
    async def _on_message_after_dispatch(self, message: BLEMessage) -> None:
        """Re-evaluate the state after every uplink message.

        ``decision_update`` is handled by ``_handle_decision_update``
        (which also runs ``self._emit("message", ...)``), so we do NOT
        call ``_maybe_transition`` here for that type.
        """

        if message.type in ("decision_update",):
            return  # already handled in _dispatch
        if message.type not in ("eye_data", "screen_data", "heartbeat"):
            return
        await self._maybe_transition()

    async def _maybe_transition(self) -> None:
        """Decide → commit.  If the state changed, fire side-effects."""

        new_state, score, prev_state, feedback = self._driver.decide()
        if new_state not in STATES:
            return
        changed = self._driver.commit(new_state, score, prev_state)
        if not changed:
            return
        if new_state == self._last_state:
            # decide() decided to stay in the same state — nothing to do.
            return
        await self._on_state_change(new_state, prev_state, score, feedback)

    async def _handle_decision_update(self, message: BLEMessage) -> None:
        """Apply the laptop's authoritative decision to local state.

        Per UNO_Q_BLE_DECISION_PROTOCOL.md the laptop is the source of
        truth for the final ``focused / distracted / ... / resting``
        state — we do NOT run the local ``StateDriver`` for this
        message type.  We still update ``focus_score`` and surface the
        new state through ``state_update`` so the Windows UI sees what
        the laptop decided.
        """

        data = message.data
        new_state = data["state"]

        score = data.get("score")
        if score is None:
            score = self._driver.focus_score
        else:
            score = max(0, min(100, int(score)))

        app = data.get("app") or ""
        if not isinstance(app, str):
            app = ""

        prev_state = self._driver.current_state
        self._driver.focus_score = score
        # Stash the app name so subsequent focus / alert renders carry
        # the current window title (the focus flow UI expects this).
        if app:
            if not isinstance(self._driver.last_screen, dict):
                self._driver.last_screen = {}
            self._driver.last_screen["name"] = app

        feedback = FEEDBACK_FOR_STATE.get(new_state, "none")

        if self._driver.commit(new_state, score, prev_state):
            await self._on_state_change(new_state, prev_state, score, feedback)

    async def _on_state_change(
        self,
        new_state: str,
        prev_state: str,
        score: int,
        feedback: str,
    ) -> None:
        """Common side-effects for every transition: wristband + TFT + downlink."""

        self._last_state = new_state
        LOGGER.info(
            "state transition: %s -> %s (score=%s, feedback=%s)",
            prev_state, new_state, score, feedback,
        )

        # 1) Per UNO_Q_BLE_DECISION_PROTOCOL.md §"同学需要新增的上行消息":
        #    "resting 必须立即停止屏幕以外的设备输出" → stop any
        #    in-progress wristband vibration when entering resting.
        if new_state == "resting":
            self._wristband.stop_vibration()

        # 2) Wristband vibration policy (user requirement #3):
        #    - distracted / procrastinating → 3 × intensity 40
        #    - waiting: per the supplement, no punitive feedback
        #    - focused / resting: rest_command handles its own edges
        if new_state in ("distracted", "procrastinating"):
            repeats = (
                VIBRATION_REPEATS_DISTRACTED
                if new_state == "distracted"
                else VIBRATION_REPEATS_PROCRASTINATING
            )
            self._vibrate_wristband(trigger="distraction", repeats=repeats)

        # 3) TFT update:
        #    - focused → show focus screen
        #    - distracted / procrastinating → show alert screen
        #    - waiting: intentionally no UI change (per "no punitive feedback")
        #    - resting: rest_command manages its own showBreakScreen
        if new_state == "focused":
            self._tft.show_focus(
                pct=int(score),
                screen=self._extract_screen_label() or "VS Code",
                status="高度专注",
            )
        elif new_state in ("distracted", "procrastinating"):
            self._tft.show_alert(screen=self._extract_screen_label() or "分心")
        # waiting and resting: leave the screen as-is.

        # 4) Downlink: push a state_update so the Windows client can
        #    log / display the transition.  The ``triggered_feedback``
        #    field reflects the new spec (resting/waiting → "none").
        try:
            await self.send_state_update(
                state=new_state,
                focus_score=int(score),
                prev_state=prev_state,
                duration_in_state=0.0,
                triggered_feedback=feedback,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("send_state_update failed: %s", exc)

    # ── rest-command override ───────────────────────────────────────
    async def _handle_rest_command(self, data: Dict[str, Any]) -> None:
        """Override to add wristband vibration + TFT update on rest edges."""

        prev_in_rest = self._rest_started_at is not None
        await super()._handle_rest_command(data)
        new_in_rest = self._rest_started_at is not None

        if new_in_rest and not prev_in_rest:
            # 进入休息 → vibrate once + show break screen.
            self._vibrate_wristband(
                trigger="rest_start", repeats=VIBRATION_REPEATS_REST_START,
            )
            remain = self._rest_duration if self._rest_duration else 0
            self._tft.show_break(remain=remain, next_sess=0)
        elif prev_in_rest and not new_in_rest:
            # 休息结束 → vibrate twice + show focus screen.
            self._vibrate_wristband(
                trigger="rest_end", repeats=VIBRATION_REPEATS_REST_END,
            )
            self._tft.show_focus(
                pct=self._driver.focus_score,
                screen=self._extract_screen_label() or "VS Code",
                status="高度专注",
            )
            self._last_state = self._driver.current_state

    # ── device_status override ──────────────────────────────────────
    def _snapshot_device_status(self) -> Dict[str, Any]:
        """Inject real wristband + TFT health instead of placeholders.

        ``eeg_*`` fields stay at their placeholder values — the
        supplement does not yet define a path for EEG status reporting
        on UNO Q, so we keep the upstream behaviour.
        """

        return {
            "eeg_connected": False,
            "eeg_battery": -1,
            "wristband_connected": bool(self._wristband.is_subscribed()),
            "wristband_battery": -1,
            "tft_display": self._tft.last_status() or "running",
        }

    # ── helpers ─────────────────────────────────────────────────────
    def _vibrate_wristband(self, *, trigger: str, repeats: int) -> None:
        """Send a vibration command and report success back over BLE."""

        if not self._wristband.is_subscribed():
            LOGGER.info(
                "vibration %s skipped: wristband not subscribed", trigger,
            )
            self._safe_create_task(self.send_vibration_feedback(
                mode="vibrate_short",
                trigger=trigger,
                success=False,
            ))
            return
        sent = self._wristband.send_vibration(self._intensity, repeats)
        LOGGER.info(
            "vibration %s x%d @intensity=%d sent=%s",
            trigger, repeats, self._intensity, sent,
        )
        self._safe_create_task(self.send_vibration_feedback(
            mode="vibrate_short",
            trigger=trigger,
            success=bool(sent),
        ))

    def _safe_create_task(self, coro) -> None:
        """Schedule ``coro`` on the running loop without raising if none."""

        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(coro)

    def _extract_screen_label(self) -> Optional[str]:
        """Return the current screen name from the last ``screen_data`` or
        the ``app`` field of the last ``decision_update``."""

        screen = self._driver.last_screen
        if isinstance(screen, dict):
            for key in ("name", "app", "window", "title", "screen"):
                value = screen.get(key)
                if isinstance(value, str) and value:
                    return value
        return None
