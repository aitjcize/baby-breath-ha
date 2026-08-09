from __future__ import annotations

import threading
import time
from pathlib import Path

from app.config import AppConfig, DebugConfig, MQTTConfig
from app.service import BabyRespirationService
from app.settings import SettingsStore


def make_service(tmp_path: Path) -> BabyRespirationService:
    config = AppConfig(mqtt=MQTTConfig(enabled=False), debug=DebugConfig(enabled=False))
    store = SettingsStore(tmp_path)
    store.update(rtsp_url="demo://breathing")
    return BabyRespirationService(config, settings_store=store)


def test_demo_end_to_end_status_and_slim_mqtt_payload(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    published: list[dict] = []
    service.mqtt.publish = published.append  # type: ignore[method-assign]

    thread = threading.Thread(target=service.run, kwargs={"run_seconds": 4.0})
    thread.start()
    time.sleep(1.5)
    # Runtime ROI change through the public API must not disturb the loop.
    result = service.apply_settings(roi=(0.4, 0.35, 0.35, 0.35))
    assert result["applied"] is True
    thread.join(timeout=15)
    assert not thread.is_alive()

    status = service.latest_status
    assert status["camera_configured"] is True
    assert status["stream_status"] == "connected"
    assert status["roi"] == [0.4, 0.35, 0.35, 0.35]
    assert "scan" in status and "waveform_y" in status

    assert published, "expected at least one MQTT publish"
    for payload in published:
        assert "waveform_y" not in payload
        assert "waveform_t" not in payload
        assert "scan" not in payload
        assert "bpm" in payload

    # The wizard's ROI edit persisted for the next start.
    assert SettingsStore(tmp_path).get().roi == (0.4, 0.35, 0.35, 0.35)


def test_mqtt_settings_resolve_effective_broker(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assert service._effective_mqtt().enabled is False  # nothing configured

    service.apply_settings(mqtt={"mode": "custom", "host": "10.0.0.9", "port": 1884, "username": "u", "password": "p"})
    effective = service._effective_mqtt()
    assert effective.enabled is True
    assert (effective.host, effective.port, effective.username) == ("10.0.0.9", 1884, "u")

    service.apply_settings(mqtt={"mode": "disabled"})
    assert service._effective_mqtt().enabled is False

    service.apply_settings(mqtt={"mode": "auto"})
    assert service._effective_mqtt() == service.config.mqtt


def test_rate_low_flag_with_hysteresis(tmp_path: Path) -> None:
    service = make_service(tmp_path)  # default threshold 20
    assert service._update_rate_low(24.0) is False
    assert service._update_rate_low(19.5) is True
    assert service._update_rate_low(21.0) is True   # inside hysteresis band
    assert service._update_rate_low(22.5) is False  # cleared at threshold + 2
    assert service._update_rate_low(None) is False


def test_probe_demo_and_unconfigured(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    result = service.probe("demo://breathing")
    assert result["ok"] is True
    assert service.probe_preview() is not None

    empty = BabyRespirationService(
        AppConfig(mqtt=MQTTConfig(enabled=False), debug=DebugConfig(enabled=False)),
        settings_store=SettingsStore(tmp_path / "fresh"),
    )
    assert empty.camera_configured is False
