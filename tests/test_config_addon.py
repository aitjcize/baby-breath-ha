from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import CameraConfig, SignalConfig, load_addon_config


def write_options(tmp_path: Path, **options) -> Path:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options), encoding="utf-8")
    return path


def test_defaults_without_mqtt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BABY_MQTT_HOST", raising=False)
    config = load_addon_config(write_options(tmp_path, log_level="info"))
    assert config.signal.min_bpm == 15
    assert config.mqtt.enabled is False
    assert config.debug.host == "0.0.0.0"
    assert config.debug.port == 8080


def test_empty_options_use_dataclass_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader must not shadow dataclass defaults with its own literals —
    a drifted fallback once silently ran the detection hold at 10 s while the
    dataclass (and everyone reading it) said 15."""
    monkeypatch.delenv("BABY_MQTT_HOST", raising=False)
    config = load_addon_config(write_options(tmp_path))
    assert config.signal == SignalConfig()
    assert config.camera.processing_fps == CameraConfig().processing_fps
    assert config.camera.target_processing_width == CameraConfig().target_processing_width
    assert config.signal.detection_hold_seconds == 15.0


def test_supervisor_mqtt_service_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BABY_MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("BABY_MQTT_PORT", "1884")
    monkeypatch.setenv("BABY_MQTT_USERNAME", "addons")
    monkeypatch.setenv("BABY_MQTT_PASSWORD", "secret")
    config = load_addon_config(write_options(tmp_path))
    assert config.mqtt.enabled is True
    assert config.mqtt.host == "core-mosquitto"
    assert config.mqtt.port == 1884
    assert config.mqtt.username == "addons"


def test_topic_options_still_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BABY_MQTT_HOST", "core-mosquitto")
    config = load_addon_config(write_options(tmp_path, mqtt_base_topic="nursery", mqtt_discovery_prefix="ha"))
    assert config.mqtt.base_topic == "nursery"
    assert config.mqtt.discovery_prefix == "ha"


def test_tuning_options_are_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BABY_MQTT_HOST", raising=False)
    config = load_addon_config(write_options(tmp_path, min_bpm=20, max_bpm=80, processing_fps=8, no_breath_timeout=30))
    assert config.signal.min_bpm == 20
    assert config.signal.max_bpm == 80
    assert config.camera.processing_fps == 8
    assert config.signal.no_breath_timeout == 30


def test_cpu_tuning_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BABY_MQTT_HOST", raising=False)
    config = load_addon_config(write_options(tmp_path, processing_fps=4, target_processing_width=256))
    assert config.camera.processing_fps == 4
    assert config.camera.target_processing_width == 256  # validates against max_bpm=90


def test_low_rate_threshold_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BABY_MQTT_HOST", raising=False)
    config = load_addon_config(write_options(tmp_path, low_rate_threshold_bpm=25))
    assert config.signal.low_rate_threshold_bpm == 25
