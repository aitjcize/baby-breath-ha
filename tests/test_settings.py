from __future__ import annotations

import json
from pathlib import Path

from app.settings import SettingsStore


def test_roundtrip_and_partial_update(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    assert store.get().rtsp_url == ""
    assert store.get().roi is None

    store.update(rtsp_url="rtsp://cam/stream")
    store.update(roi=(0.2, 0.3, 0.4, 0.3))
    reloaded = SettingsStore(tmp_path).get()
    assert reloaded.rtsp_url == "rtsp://cam/stream"
    assert reloaded.roi == (0.2, 0.3, 0.4, 0.3)

    store.update(rtsp_url="rtsp://cam/other")
    assert SettingsStore(tmp_path).get().roi == (0.2, 0.3, 0.4, 0.3)


def test_mqtt_settings_roundtrip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    assert store.get().mqtt_mode == "auto"
    store.update(mqtt_mode="custom", mqtt_host="10.0.0.5", mqtt_port=8883, mqtt_username="u", mqtt_password="p")
    reloaded = SettingsStore(tmp_path).get()
    assert reloaded.mqtt_mode == "custom"
    assert reloaded.mqtt_host == "10.0.0.5"
    assert reloaded.mqtt_port == 8883
    store.update(mqtt_mode="disabled")
    assert SettingsStore(tmp_path).get().mqtt_host == "10.0.0.5"  # fields survive mode flips

    import pytest as _pytest
    with _pytest.raises(ValueError):
        store.update(mqtt_mode="bogus")
    with _pytest.raises(ValueError):
        store.update(mqtt_port=0)


def test_monitoring_toggle_roundtrip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    assert store.get().monitoring_enabled is True
    store.update(monitoring_enabled=False)
    assert SettingsStore(tmp_path).get().monitoring_enabled is False


def test_corrupt_or_invalid_content_starts_unconfigured(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert SettingsStore(tmp_path).get().rtsp_url == ""

    (tmp_path / "settings.json").write_text(
        json.dumps({"rtsp_url": "rtsp://cam/stream", "roi": [2, 2, -1, 0]}), encoding="utf-8"
    )
    settings = SettingsStore(tmp_path).get()
    assert settings.rtsp_url == "rtsp://cam/stream"
    assert settings.roi is None
