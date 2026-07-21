#!/usr/bin/env python3
"""Hierarchical learning-focus decision logic.

Screen context decides whether the current activity is learning-related.
Camera context then decides whether the learner is present and facing the
computer.  EEG is used only after both gates pass, where it is reported as a
percentage instead of being asked to distinguish study from an engaging
distraction such as a phone.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Mapping, Optional


LEARNING_SCREEN_STATES = {
    "focused",
    "focused_work",
    "study",
    "work",
    "learning",
    "casual_browse",
    "专注工作",
    "一般浏览",
    "学习",
}
NON_LEARNING_SCREEN_STATES = {
    "slacking",
    "procrastinating",
    "entertainment",
    "away",
    "摸鱼",
    "离开",
}


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "focused", "present"}:
        return True
    if normalized in {"0", "false", "no", "distracted", "away"}:
        return False
    return None


@dataclass
class _TimedPayload:
    data: dict
    received_at: float


class FocusDecisionEngine:
    """Apply screen -> camera -> EEG gates in that strict order."""

    def __init__(
        self,
        *,
        screen_stale_seconds: float = 150.0,
        camera_stale_seconds: float = 5.0,
        eeg_stale_seconds: float = 10.0,
        camera_hold_seconds: float = 3.0,
    ) -> None:
        self.screen_stale_seconds = screen_stale_seconds
        self.camera_stale_seconds = camera_stale_seconds
        self.eeg_stale_seconds = eeg_stale_seconds
        self.camera_hold_seconds = camera_hold_seconds
        self._screen: Optional[_TimedPayload] = None
        self._camera: Optional[_TimedPayload] = None
        self._eeg: Optional[_TimedPayload] = None

    def reset(self) -> None:
        """Discard pre-rest inputs so monitoring resumes from fresh samples."""
        self._screen = None
        self._camera = None
        self._eeg = None

    def latest_screen_context(self) -> dict:
        """Return a copy suitable for an external display/actuator bridge."""
        return dict(self._screen.data) if self._screen is not None else {}

    def update_screen(self, payload: Mapping[str, Any], *, now: Optional[float] = None) -> None:
        self._screen = _TimedPayload(dict(payload), time.monotonic() if now is None else now)

    def update_camera(self, payload: Mapping[str, Any], *, now: Optional[float] = None) -> None:
        self._camera = _TimedPayload(dict(payload), time.monotonic() if now is None else now)

    def update_eeg(
        self,
        focus_percent: Optional[float],
        *,
        valid: bool,
        source: str,
        reason: str = "",
        now: Optional[float] = None,
    ) -> None:
        percent = None
        if focus_percent is not None:
            percent = max(0.0, min(100.0, float(focus_percent)))
        self._eeg = _TimedPayload(
            {
                "focus_percent": percent,
                "valid": bool(valid),
                "source": source,
                "reason": reason,
            },
            time.monotonic() if now is None else now,
        )

    @staticmethod
    def _fresh(item: Optional[_TimedPayload], max_age: float, now: float) -> bool:
        return item is not None and now - item.received_at <= max_age

    def _screen_is_learning(self) -> Optional[bool]:
        if self._screen is None:
            return None
        data = self._screen.data
        explicit = _optional_bool(data.get("is_learning"))
        if explicit is not None:
            return explicit
        state = str(data.get("state", "")).strip().lower()
        if state in LEARNING_SCREEN_STATES:
            return True
        if state in NON_LEARNING_SCREEN_STATES:
            return False
        return None

    def evaluate(self, *, now: Optional[float] = None, ts: Optional[float] = None) -> dict:
        now_value = time.monotonic() if now is None else now
        result = {
            "type": "focus_decision",
            "ts": round(float(ts if ts is not None else 0.0), 2),
            "state": "waiting_screen",
            "label": "等待屏幕判断",
            "focus_percent": None,
            "reason": "尚未收到屏幕学习状态",
        }

        if not self._fresh(self._screen, self.screen_stale_seconds, now_value):
            if self._screen is not None:
                result["reason"] = "屏幕判断已过期"
            return result

        learning = self._screen_is_learning()
        if learning is False:
            result.update(state="slacking", label="摸鱼", reason="屏幕内容不属于学习任务")
            return result
        if learning is None:
            result.update(state="waiting_screen", label="等待屏幕判断", reason="屏幕状态无法确认是否学习")
            return result

        if not self._fresh(self._camera, self.camera_stale_seconds, now_value):
            result.update(state="waiting_camera", label="等待摄像头判断", reason="屏幕正在学习，但摄像头状态缺失或已过期")
            return result

        camera = self._camera.data
        face_detected = _optional_bool(camera.get("face_detected"))
        if face_detected is None and camera.get("confidence") is not None:
            try:
                face_detected = float(camera["confidence"]) >= 0.5
            except (TypeError, ValueError):
                pass
        looking = _optional_bool(camera.get("looking_at_screen"))
        if looking is None:
            looking = _optional_bool(camera.get("is_focused"))
        if looking is None:
            camera_state = str(camera.get("state", "")).strip().lower()
            if camera_state in {"focused", "专注"}:
                looking = True
            elif camera_state in {"distracted", "走神", "away", "离开"}:
                looking = False
        try:
            state_duration = max(0.0, float(camera.get("state_duration", 0.0) or 0.0))
        except (TypeError, ValueError):
            state_duration = 0.0

        camera_bad = face_detected is False or looking is False
        if camera_bad and state_duration < self.camera_hold_seconds:
            result.update(
                state="checking_camera",
                label="确认中",
                reason=f"检测到短暂偏离，持续 {state_duration:.1f}s",
            )
            return result
        if camera_bad:
            reason = "未检测到人脸" if face_detected is False else "视线持续偏离电脑"
            result.update(state="distracted", label="走神", reason=reason)
            return result
        if face_detected is None or looking is None:
            result.update(state="waiting_camera", label="等待摄像头判断", reason="摄像头字段不完整")
            return result

        if not self._fresh(self._eeg, self.eeg_stale_seconds, now_value):
            result.update(state="waiting_eeg", label="等待脑电数据", reason="屏幕和摄像头已通过")
            return result

        eeg = self._eeg.data
        if not eeg["valid"] or eeg["focus_percent"] is None:
            result.update(
                state="eeg_invalid",
                label="脑电信号无效",
                reason=eeg.get("reason") or "当前脑电窗口质量不足",
                eeg_source=eeg.get("source"),
            )
            return result

        percent = round(float(eeg["focus_percent"]), 1)
        result.update(
            state="focused",
            label=f"专注：{percent:.1f}%",
            focus_percent=percent,
            reason="屏幕学习、人在场且看向电脑，显示脑电投入度",
            eeg_source=eeg.get("source"),
        )
        return result
