from __future__ import annotations

import json
import os
import re
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ADDON_OPTIONS_PATH = Path("/data/options.json")


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
    minimum_signal_rms: float = 0.001
    minimum_snr_db: float = 3.0
    # Samples deviating more than this many MADs from the window median are
    # transients (twitches, brushed limbs) and are masked out; 0 disables.
    transient_mad_threshold: float = 6.0
    minimum_image_contrast: float = 3.0
    minimum_sharpness: float = 1.0
    maximum_interpolation_gap: float = 1.0
    baseline_required_duration: float = 10.0
    presence_enabled: bool = True


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


def load_addon_config(options_path: str | Path = ADDON_OPTIONS_PATH) -> AppConfig:
    """Build the configuration from Home Assistant add-on options.

    Supervisor-provided MQTT service credentials arrive through the
    ``BABY_MQTT_*`` environment variables exported by run.sh; explicit broker
    options take precedence over them.
    """
    with Path(options_path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("add-on options root must be a mapping")

    camera = CameraConfig(
        processing_fps=float(raw.get("processing_fps", 5)),
        target_processing_width=int(raw.get("target_processing_width", 320)),
    )
    signal = SignalConfig(
        min_bpm=float(raw.get("min_bpm", 15)),
        max_bpm=float(raw.get("max_bpm", 90)),
        minimum_confidence=float(raw.get("minimum_confidence", 55)),
        no_breath_timeout=float(raw.get("no_breath_timeout", 12)),
        minimum_signal_rms=float(raw.get("minimum_signal_rms", 0.001)),
        presence_enabled=bool(raw.get("presence_detection", True)),
    )
    config = AppConfig(
        camera=camera,
        signal=signal,
        mqtt=_addon_mqtt(raw),
        debug=DebugConfig(enabled=True, host="0.0.0.0", port=8080),
        log_level=str(raw.get("log_level", "info")).upper(),
    )
    validate_config(config)
    return config


def _addon_mqtt(raw: dict[str, Any]) -> MQTTConfig:
    base_topic = str(raw.get("mqtt_base_topic", "baby_respiration")).strip() or "baby_respiration"
    discovery_prefix = str(raw.get("mqtt_discovery_prefix", "homeassistant")).strip() or "homeassistant"
    if raw.get("mqtt_custom_broker"):
        host = str(raw.get("mqtt_host", "")).strip()
        return MQTTConfig(
            enabled=bool(host),
            host=host or "localhost",
            port=int(raw.get("mqtt_port", 1883)),
            username=str(raw.get("mqtt_username", "")),
            password=str(raw.get("mqtt_password", "")),
            base_topic=base_topic,
            discovery_prefix=discovery_prefix,
        )
    env_host = os.environ.get("BABY_MQTT_HOST", "").strip()
    return MQTTConfig(
        enabled=bool(env_host),
        host=env_host or "localhost",
        port=int(os.environ.get("BABY_MQTT_PORT", "1883") or 1883),
        username=os.environ.get("BABY_MQTT_USERNAME", ""),
        password=os.environ.get("BABY_MQTT_PASSWORD", ""),
        base_topic=base_topic,
        discovery_prefix=discovery_prefix,
    )


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
