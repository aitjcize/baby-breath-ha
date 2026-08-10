from __future__ import annotations

import cv2
import numpy as np

from app.config import CameraConfig, SignalConfig
from app.motion import DenseOpticalFlowExtractor


def test_duplicate_frames_become_missing_samples() -> None:
    """A relay stall repeating frames must not inject zero-motion samples."""
    rng = np.random.default_rng(3)
    base = cv2.cvtColor(rng.integers(30, 225, size=(120, 160), dtype=np.uint8), cv2.COLOR_GRAY2BGR)
    extractor = DenseOpticalFlowExtractor(
        CameraConfig(target_processing_width=160, processing_fps=5),
        SignalConfig(),
    )
    extractor.process(base, 0.0)  # warmup
    duplicate, _ = extractor.process(base.copy(), 0.2)
    assert not duplicate.valid
    assert duplicate.value is None
    assert duplicate.reason == "duplicate_frame"
    # Sustained repetition escalates to the frozen-video reason.
    frozen = None
    for index in range(12):
        frozen, _ = extractor.process(base.copy(), 0.4 + index * 0.2)
    assert frozen is not None and frozen.reason == "frozen_video"


def test_dense_flow_extracts_local_vertical_roi_motion() -> None:
    rng = np.random.default_rng(7)
    base_gray = rng.integers(30, 225, size=(120, 160), dtype=np.uint8)
    base = cv2.cvtColor(base_gray, cv2.COLOR_GRAY2BGR)
    moved = base.copy()
    x0, y0, x1, y1 = 40, 42, 120, 84
    patch = base[y0:y1, x0:x1]
    transform = np.float32([[1, 0, 0], [0, 1, 0.7]])
    moved[y0:y1, x0:x1] = cv2.warpAffine(patch, transform, (x1 - x0, y1 - y0), borderMode=cv2.BORDER_REFLECT)

    extractor = DenseOpticalFlowExtractor(
        CameraConfig(target_processing_width=160, processing_fps=5, roi=(0.25, 0.35, 0.5, 0.35)),
        SignalConfig(excessive_motion_threshold=10),
    )
    warmup, _ = extractor.process(base, 0.0)
    observation, overlay = extractor.process(moved, 0.2)

    assert not warmup.valid
    assert observation.valid
    assert observation.value is not None
    assert observation.value > 0.15
    assert observation.global_motion < 0.1
    assert overlay.shape == base.shape
