# EEG baseline v3 training report

Generated from whole-session splits on 2026-07-21. Validation and test rows
were not used to fit preprocessing or model weights.

## Data used

- Training: 773 usable windows (424 focus, 349 distraction) from four sessions.
- New `session_20260721_021145.jsonl`: 13 usable labeled windows. Most valid EEG
  occurred before the first label, so those unlabeled windows were excluded.
- New `session_20260721_023408.jsonl`: 116 usable labeled windows.
- Validation: `local_session_20260720_200415.jsonl`, 93 usable windows.
- Test: `local_session_20260720_214422.jsonl`, 333 usable windows.

All invalid/flatline and unlabeled windows were excluded, as were windows within
five seconds of a marker transition.

## Independent-session results

| Model | Validation balanced accuracy | Validation AUC | Test balanced accuracy | Test AUC |
|---|---:|---:|---:|---:|
| v2 | 0.769 | 0.794 | 0.616 | 0.594 |
| v3 | 0.842 | 0.861 | 0.609 | 0.626 |

The v3 model improves probability ranking (AUC) on both held-out sessions and
keeps test balanced accuracy close to v2. The selected probability threshold is
0.55. This remains a preliminary single-person model; more complete labeled
sessions are needed for a reliable generalization estimate.

ONNX verification passed with maximum PyTorch probability difference
`5.96e-08`.
