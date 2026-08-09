from __future__ import annotations

import os
import re
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CameraConfig:
    rtsp_url: str = ""
    reconnect_interval: float = 5.0
    target_processing_width: int = 320
    processing_fps: float = 5.0
    roi: tuple[float, float, float, float] = (0.25, 0.35, 0.5, 0.35)
    open_timeout_ms: int = 8000
    read_timeout_ms: int = 8000


@dataclass(frozen=True)
class SignalConfig:
    min_bpm: float = 15.0
    max_bpm: float = 90.0
    analysis_window_duration: float = 24.0
    minimum_valid_window_duration: float = 15.0
    minimum_confidence: float = 55.0
    no_breath_timeout: float = 12.0
    measurement_invalid_timeout: float = 8.0
    excessive_motion_threshold: float = 1.5
    minimum_signal_rms: float = 0.003
    minimum_snr_db: float = 3.0
    minimum_image_contrast: float = 3.0
    minimum_sharpness: float = 1.0
    maximum_interpolation_gap: float = 1.0
    baseline_required_duration: float = 10.0


@dataclass(frozen=True)
class MQTTConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    base_topic: str = "baby_respiration"
    discovery_prefix: str = "homeassistant"
    client_id: str = "baby-respiration-detector"
    tls: bool = False


@dataclass(frozen=True)
class DebugConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class AppConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    log_level: str = "INFO"


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: os.environ.get(match.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ValueError(f"config section {name!r} must be a mapping")
    return section


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    raw = _expand_env(raw)

    camera_data = _section(raw, "camera")
    if "roi" in camera_data:
        roi = camera_data["roi"]
        if not isinstance(roi, list) or len(roi) != 4:
            raise ValueError("camera.roi must be [x, y, width, height]")
        camera_data["roi"] = tuple(float(value) for value in roi)

    debug_data = _section(raw, "debug")
    if os.environ.get("BABY_DEBUG_HOST"):
        debug_data["host"] = os.environ["BABY_DEBUG_HOST"]
    config = AppConfig(
        camera=CameraConfig(**camera_data),
        signal=SignalConfig(**_section(raw, "signal")),
        mqtt=MQTTConfig(**_section(raw, "mqtt")),
        debug=DebugConfig(**debug_data),
        log_level=str(raw.get("log_level", "INFO")),
    )
    validate_config(config)
    return config


def normalize_roi(values: Any) -> tuple[float, float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError("ROI must contain [x, y, width, height]")
    try:
        roi = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError("ROI values must be numbers") from exc
    if not all(math.isfinite(value) for value in roi):
        raise ValueError("ROI values must be finite")
    x, y, width, height = roi
    if min(x, y) < 0 or width < 0.01 or height < 0.01:
        raise ValueError("ROI must have a minimum normalized width and height of 0.01")
    if x + width > 1.000001 or y + height > 1.000001:
        raise ValueError("ROI must fit within normalized image coordinates")
    return tuple(round(value, 4) for value in roi)  # type: ignore[return-value]


def save_roi(path: str | Path, values: Any) -> tuple[float, float, float, float]:
    """Update only camera.roi, preserving the rest of the YAML and its comments."""
    roi = normalize_roi(values)
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    camera_index: int | None = None
    camera_indent = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(?P<indent>\s*)camera\s*:\s*(?:#.*)?(?:\r?\n)?$", line)
        if match:
            camera_index = index
            camera_indent = len(match.group("indent"))
            break
    if camera_index is None:
        raise ValueError("configuration has no camera section")

    section_end = len(lines)
    roi_index: int | None = None
    roi_indent = " " * (camera_indent + 2)
    for index in range(camera_index + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip())
        if indentation <= camera_indent:
            section_end = index
            break
        match = re.match(r"^(?P<indent>\s*)roi\s*:.*?(?:\r?\n)?$", line)
        if match:
            roi_index = index
            roi_indent = match.group("indent")
            break

    newline = "\r\n" if "\r\n" in text else "\n"
    formatted = ", ".join(f"{value:.4f}" for value in roi)
    replacement = f"{roi_indent}roi: [{formatted}]{newline}"
    if roi_index is None:
        lines.insert(section_end, replacement)
    else:
        if not lines[roi_index].endswith(("\n", "\r")):
            replacement = replacement.rstrip("\r\n")
        lines[roi_index] = replacement
    config_path.write_text("".join(lines), encoding="utf-8")
    return roi


def validate_config(config: AppConfig) -> None:
    normalize_roi(config.camera.roi)
    if config.camera.target_processing_width < 64:
        raise ValueError("camera.target_processing_width must be at least 64")
    if config.camera.processing_fps < 2 * config.signal.max_bpm / 60:
        raise ValueError("camera.processing_fps must exceed twice signal.max_bpm in Hz")
    if not 0 < config.signal.min_bpm < config.signal.max_bpm:
        raise ValueError("signal BPM range is invalid")
    if config.signal.minimum_valid_window_duration > config.signal.analysis_window_duration:
        raise ValueError("minimum valid window cannot exceed analysis window")
    if not 0 <= config.signal.minimum_confidence <= 100:
        raise ValueError("minimum confidence must be between 0 and 100")
    if config.signal.minimum_snr_db < 0:
        raise ValueError("minimum SNR must be non-negative")
    if not 1 <= config.mqtt.port <= 65535:
        raise ValueError("mqtt.port is invalid")
    if not 1 <= config.debug.port <= 65535:
        raise ValueError("debug.port is invalid")
