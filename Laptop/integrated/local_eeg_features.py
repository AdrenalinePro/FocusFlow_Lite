#!/usr/bin/env python3
"""Local EEG spectral feature extraction for FocusFlow Lite.

This module intentionally has no AffectiveCloud dependency.  It accepts an
already decoded left/right EEG waveform and produces the five EEG features
defined by the project proposal:

    theta, alpha, beta, theta / beta, (theta + alpha) / beta

The implementation only depends on NumPy so it stays lightweight enough for
the UNO Q Linux side.  It is usable both offline and from a future real-time
BLE packet decoder.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Optional

import numpy as np


@dataclass(frozen=True)
class FeatureConfig:
    sample_rate: int = 250
    window_seconds: float = 5.0
    step_seconds: float = 1.0
    low_frequency: float = 1.0
    high_frequency: float = 45.0
    max_abs_amplitude: float = 500.0
    min_channel_std: float = 0.05
    # AffectiveCloud/Flowtime contact quality increases from 0 (no signal) to
    # 4 (best).  Accept 2/3/4 and reject 0/1.  This direction is also visible
    # in recordings: quality 0 blocks are all-zero while 3/4 carry EEG waves.
    min_quality: int = 2

    @property
    def window_samples(self) -> int:
        return int(round(self.sample_rate * self.window_seconds))

    @property
    def step_samples(self) -> int:
        return int(round(self.sample_rate * self.step_seconds))


@dataclass(frozen=True)
class EEGFeatures:
    theta: float
    alpha: float
    beta: float
    theta_beta: float
    theta_alpha_beta: float
    delta: float
    gamma: float
    left_std: float
    right_std: float
    channel_correlation: float
    valid: bool
    invalid_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


BANDS = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 45.0),
}


def _invalid(reason: str, left_std: float = 0.0, right_std: float = 0.0) -> EEGFeatures:
    return EEGFeatures(
        theta=0.0,
        alpha=0.0,
        beta=0.0,
        theta_beta=0.0,
        theta_alpha_beta=0.0,
        delta=0.0,
        gamma=0.0,
        left_std=left_std,
        right_std=right_std,
        channel_correlation=0.0,
        valid=False,
        invalid_reason=reason,
    )


def _detrend(values: np.ndarray) -> np.ndarray:
    """Remove the least-squares straight line without requiring SciPy."""
    x = np.arange(values.size, dtype=np.float64)
    x -= x.mean()
    centered = values - values.mean()
    denominator = float(np.dot(x, x))
    slope = float(np.dot(x, centered) / denominator) if denominator else 0.0
    return centered - slope * x


def _periodogram(values: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Return a Hann-windowed one-sided power spectral density estimate."""
    detrended = _detrend(values)
    window = np.hanning(detrended.size)
    weighted = detrended * window
    spectrum = np.fft.rfft(weighted)
    scale = sample_rate * float(np.square(window).sum())
    psd = np.square(np.abs(spectrum)) / max(scale, np.finfo(float).eps)
    if psd.size > 2:
        psd[1:-1] *= 2.0
    frequencies = np.fft.rfftfreq(detrended.size, d=1.0 / sample_rate)
    return frequencies, psd


def _integrated_power(
    frequencies: np.ndarray,
    psd: np.ndarray,
    low: float,
    high: float,
) -> float:
    # Use a half-open interval so adjacent bands never double-count a bin.
    mask = (frequencies >= low) & (frequencies < high)
    if not np.any(mask):
        return 0.0
    return float(np.trapezoid(psd[mask], frequencies[mask]))


def extract_features(
    eeg_left: Iterable[float],
    eeg_right: Iterable[float],
    *,
    quality: Optional[int] = None,
    config: FeatureConfig = FeatureConfig(),
) -> EEGFeatures:
    """Extract relative band powers and the project's five EEG features.

    ``quality`` is optional because the fully local path may not expose the
    proprietary cloud quality value.  When present, values below 2 are
    rejected. Independent flat-line, finite-value and amplitude checks are
    always applied.
    """
    left = np.asarray(list(eeg_left), dtype=np.float64)
    right = np.asarray(list(eeg_right), dtype=np.float64)

    if left.ndim != 1 or right.ndim != 1 or left.size != right.size:
        return _invalid("channel_length_mismatch")
    if left.size < config.window_samples:
        return _invalid("window_too_short")
    if quality is not None and quality < config.min_quality:
        return _invalid(f"poor_device_quality:{quality}")
    if not np.all(np.isfinite(left)) or not np.all(np.isfinite(right)):
        return _invalid("non_finite_samples")

    # Always analyze exactly the most recent configured window.
    left = left[-config.window_samples :]
    right = right[-config.window_samples :]
    left_std = float(np.std(left))
    right_std = float(np.std(right))
    if left_std < config.min_channel_std or right_std < config.min_channel_std:
        return _invalid("flat_line", left_std, right_std)
    if max(float(np.max(np.abs(left))), float(np.max(np.abs(right)))) > config.max_abs_amplitude:
        return _invalid("amplitude_outlier", left_std, right_std)

    left_freq, left_psd = _periodogram(left, config.sample_rate)
    right_freq, right_psd = _periodogram(right, config.sample_rate)
    if not np.array_equal(left_freq, right_freq):
        return _invalid("frequency_axis_mismatch", left_std, right_std)
    mean_psd = (left_psd + right_psd) / 2.0

    total = _integrated_power(
        left_freq,
        mean_psd,
        config.low_frequency,
        config.high_frequency,
    )
    if total <= np.finfo(float).eps:
        return _invalid("zero_spectral_power", left_std, right_std)

    relative = {
        name: _integrated_power(left_freq, mean_psd, low, high) / total
        for name, (low, high) in BANDS.items()
    }
    beta = relative["beta"]
    epsilon = 1e-12
    correlation = float(np.corrcoef(left, right)[0, 1])
    if not np.isfinite(correlation):
        correlation = 0.0

    return EEGFeatures(
        theta=relative["theta"],
        alpha=relative["alpha"],
        beta=beta,
        theta_beta=relative["theta"] / max(beta, epsilon),
        theta_alpha_beta=(relative["theta"] + relative["alpha"]) / max(beta, epsilon),
        delta=relative["delta"],
        gamma=relative["gamma"],
        left_std=left_std,
        right_std=right_std,
        channel_correlation=correlation,
        valid=True,
    )


class StreamingFeatureExtractor:
    """Accumulate decoded EEG blocks and emit one feature row per step."""

    def __init__(self, config: FeatureConfig = FeatureConfig()):
        self.config = config
        self._left = np.empty(0, dtype=np.float64)
        self._right = np.empty(0, dtype=np.float64)
        self._buffer_start_sample = 0
        self._total_samples = 0
        self._next_emit_sample = config.window_samples

    def reset(self) -> None:
        self._left = np.empty(0, dtype=np.float64)
        self._right = np.empty(0, dtype=np.float64)
        self._buffer_start_sample = 0
        self._total_samples = 0
        self._next_emit_sample = self.config.window_samples

    def add_block(
        self,
        eeg_left: Iterable[float],
        eeg_right: Iterable[float],
        *,
        quality: Optional[int] = None,
    ) -> list[EEGFeatures]:
        left = np.asarray(list(eeg_left), dtype=np.float64)
        right = np.asarray(list(eeg_right), dtype=np.float64)
        if left.ndim != 1 or right.ndim != 1 or left.size != right.size:
            self.reset()
            return [_invalid("channel_length_mismatch")]
        if quality is not None and quality < self.config.min_quality:
            self.reset()
            # Emit an explicit invalid row so the dashboard explains why
            # prediction paused instead of silently leaving an old result up.
            return [_invalid(f"poor_device_quality:{quality}")]

        self._left = np.concatenate((self._left, left))
        self._right = np.concatenate((self._right, right))
        self._total_samples += left.size
        emitted: list[EEGFeatures] = []

        while self._total_samples >= self._next_emit_sample:
            end = self._next_emit_sample - self._buffer_start_sample
            start = end - self.config.window_samples
            emitted.append(
                extract_features(
                    self._left[start:end],
                    self._right[start:end],
                    quality=quality,
                    config=self.config,
                )
            )
            self._next_emit_sample += self.config.step_samples

        # Discard samples that cannot participate in the next window while
        # retaining absolute indices for correct overlapping-window slicing.
        retain_from = max(0, self._next_emit_sample - self.config.window_samples)
        drop = retain_from - self._buffer_start_sample
        if drop > 0:
            self._left = self._left[drop:]
            self._right = self._right[drop:]
            self._buffer_start_sample = retain_from
        return emitted
