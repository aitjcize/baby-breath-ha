from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import load_addon_config


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


def test_custom_broker_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BABY_MQTT_HOST", "core-mosquitto")
    config = load_addon_config(
        write_options(
            tmp_path,
            mqtt_custom_broker=True,
            mqtt_host="10.0.0.5",
            mqtt_port=8883,
            mqtt_username="user",
            mqtt_password="pw",
        )
    )
    assert config.mqtt.enabled is True
    assert config.mqtt.host == "10.0.0.5"
    assert config.mqtt.port == 8883


def test_tuning_options_are_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BABY_MQTT_HOST", raising=False)
    config = load_addon_config(write_options(tmp_path, min_bpm=20, max_bpm=80, processing_fps=8, no_breath_timeout=30))
    assert config.signal.min_bpm == 20
    assert config.signal.max_bpm == 80
    assert config.camera.processing_fps == 8
    assert config.signal.no_breath_timeout == 30
