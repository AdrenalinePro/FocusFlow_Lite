#!/usr/bin/env python3
"""Inference wrapper for the project's personalized five-feature EEG model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy as np


@dataclass(frozen=True)
class EEGPrediction:
    focus_probability: float
    distraction_probability: float
    threshold: float
    state: str

    def to_dict(self) -> dict:
        return {
            "focus_probability": self.focus_probability,
            "distraction_probability": self.distraction_probability,
            "threshold": self.threshold,
            "state": self.state,
        }


class PersonalEEGModel:
    """Load preprocessing metadata and run the exported ONNX classifier.

    The model label mapping is:
      focus=0, distraction=1
    Therefore the ONNX sigmoid output is the distraction probability.
    """

    def __init__(self, artifact_dir: Path):
        artifact_dir = Path(artifact_dir)
        metadata_path = artifact_dir / "preprocessing.json"
        model_path = artifact_dir / "eeg_baseline.onnx"

        with metadata_path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)

        self.feature_order = tuple(metadata["feature_order"])
        expected = (
            "theta",
            "alpha",
            "beta",
            "theta_beta",
            "theta_alpha_beta",
        )
        if self.feature_order != expected:
            raise ValueError(
                f"Unexpected model feature order: {self.feature_order!r}"
            )

        self.clip_lower = np.asarray(metadata["raw_clip_lower"], dtype=np.float32)
        self.clip_upper = np.asarray(metadata["raw_clip_upper"], dtype=np.float32)
        self.mean = np.asarray(
            metadata["mean_after_clip_and_log"], dtype=np.float32
        )
        self.std = np.asarray(
            metadata["std_after_clip_and_log"], dtype=np.float32
        )
        log_features = set(metadata.get("log1p_features", ()))
        self.log1p_indices = tuple(
            index for index, name in enumerate(self.feature_order) if name in log_features
        )
        self.threshold = float(metadata["distraction_probability_threshold"])

        import onnxruntime as ort

        self._session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        self._input_name = self._session.get_inputs()[0].name
        self.model_name = artifact_dir.name

    def preprocess(self, values: Mapping[str, float]) -> np.ndarray:
        row = np.asarray(
            [[float(values[name]) for name in self.feature_order]],
            dtype=np.float32,
        )
        row = np.clip(row, self.clip_lower, self.clip_upper)
        if self.log1p_indices:
            row[:, self.log1p_indices] = np.log1p(
                np.maximum(row[:, self.log1p_indices], 0.0)
            )
        return ((row - self.mean) / self.std).astype(np.float32)

    def predict(self, values: Mapping[str, float]) -> EEGPrediction:
        inputs = self.preprocess(values)
        output = self._session.run(None, {self._input_name: inputs})[0]
        distraction = float(np.asarray(output).reshape(-1)[0])
        distraction = min(1.0, max(0.0, distraction))
        return EEGPrediction(
            focus_probability=1.0 - distraction,
            distraction_probability=distraction,
            threshold=self.threshold,
            state="distraction" if distraction >= self.threshold else "focus",
        )
