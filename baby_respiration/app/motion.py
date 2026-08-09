from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app.config import CameraConfig, SignalConfig


@dataclass(frozen=True)
class MotionObservation:
    timestamp: float
    value: float | None
    valid: bool
    excessive_motion: bool
    global_motion: float
    local_motion: float
    image_contrast: float
    sharpness: float
    brightness: float
    frame_change: float
    reason: str


class DenseOpticalFlowExtractor:
    """Extracts robust vertical ROI motion after subtracting camera motion."""

    def __init__(self, camera: CameraConfig, signal: SignalConfig) -> None:
        self.camera = camera
        self.signal = signal
        self._previous_gray: np.ndarray | None = None
        self._frozen_frames = 0

    def reset(self) -> None:
        self._previous_gray = None
        self._frozen_frames = 0

    def _prepare(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        height, width = frame.shape[:2]
        scale = min(1.0, self.camera.target_processing_width / width)
        if scale < 1.0:
            frame = cv2.resize(
                frame,
                (self.camera.target_processing_width, max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return frame, gray

    def roi_pixels(self, shape: tuple[int, ...]) -> tuple[int, int, int, int]:
        height, width = shape[:2]
        x, y, roi_width, roi_height = self.camera.roi
        x0 = max(0, min(width - 1, round(x * width)))
        y0 = max(0, min(height - 1, round(y * height)))
        x1 = max(x0 + 1, min(width, round((x + roi_width) * width)))
        y1 = max(y0 + 1, min(height, round((y + roi_height) * height)))
        return x0, y0, x1, y1

    def process(self, frame: np.ndarray, timestamp: float) -> tuple[MotionObservation, np.ndarray]:
        display, gray = self._prepare(frame)
        x0, y0, x1, y1 = self.roi_pixels(gray.shape)
        roi_gray = gray[y0:y1, x0:x1]
        contrast = float(np.std(roi_gray))
        sharpness = float(cv2.Laplacian(roi_gray, cv2.CV_32F).var())
        brightness = float(np.mean(roi_gray))

        overlay = display.copy()
        cv2.rectangle(overlay, (x0, y0), (x1 - 1, y1 - 1), (0, 255, 255), 2)

        if self._previous_gray is None or self._previous_gray.shape != gray.shape:
            self._previous_gray = gray
            return (
                MotionObservation(
                    timestamp, None, False, False, 0.0, 0.0,
                    contrast, sharpness, brightness, 0.0, "optical_flow_warmup",
                ),
                overlay,
            )

        previous = self._previous_gray
        self._previous_gray = gray
        frame_change = float(np.mean(cv2.absdiff(previous, gray)))
        if frame_change < 0.01:
            self._frozen_frames += 1
        else:
            self._frozen_frames = 0

        flow = cv2.calcOpticalFlowFarneback(
            previous,
            gray,
            None,
            pyr_scale=0.5,
            levels=3,
            winsize=19,
            iterations=2,
            poly_n=5,
            poly_sigma=1.1,
            flags=0,
        )
        global_dx = float(np.median(flow[..., 0]))
        global_dy = float(np.median(flow[..., 1]))
        global_motion = float(np.hypot(global_dx, global_dy))

        roi_flow = flow[y0:y1, x0:x1]
        residual_x = roi_flow[..., 0] - global_dx
        residual_y = roi_flow[..., 1] - global_dy
        vertical_motion = float(np.median(residual_y))
        residual_magnitude = np.hypot(residual_x, residual_y)
        local_motion = float(np.percentile(residual_magnitude, 75))
        motion_metric = max(global_motion, local_motion)
        excessive = motion_metric > self.signal.excessive_motion_threshold

        reasons: list[str] = []
        if excessive:
            reasons.append("excessive_motion")
        if contrast < self.signal.minimum_image_contrast:
            reasons.append("low_contrast")
        if sharpness < self.signal.minimum_sharpness:
            reasons.append("poor_focus")
        if brightness <= 3 or brightness >= 252:
            reasons.append("bad_exposure")
        if self._frozen_frames >= max(3, round(self.camera.processing_fps * 2)):
            reasons.append("frozen_video")

        valid = not reasons
        color = (0, 200, 0) if valid else (0, 0, 255)
        cv2.rectangle(overlay, (x0, y0), (x1 - 1, y1 - 1), color, 2)
        cv2.putText(
            overlay,
            "ROI " + ("valid" if valid else ", ".join(reasons)),
            (x0, max(18, y0 - 7)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            color,
            1,
            cv2.LINE_AA,
        )
        return (
            MotionObservation(
                timestamp=timestamp,
                value=vertical_motion,
                valid=valid,
                excessive_motion=excessive,
                global_motion=global_motion,
                local_motion=local_motion,
                image_contrast=contrast,
                sharpness=sharpness,
                brightness=brightness,
                frame_change=frame_change,
                reason="ok" if valid else ",".join(reasons),
            ),
            overlay,
        )
