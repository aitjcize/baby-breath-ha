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

    # Telemetry CSV captured the run.
    telemetry = list(tmp_path.glob("telemetry-*.csv"))
    assert telemetry and len(telemetry[0].read_text().splitlines()) >= 3


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


def test_monitoring_pause_and_resume(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.settings.update(monitoring_enabled=False)
    service = BabyRespirationService(
        AppConfig(mqtt=MQTTConfig(enabled=False), debug=DebugConfig(enabled=False)),
        settings_store=service.settings,
    )
    published: list[dict] = []
    service.mqtt.publish = published.append  # type: ignore[method-assign]
    thread = threading.Thread(target=service.run, kwargs={"run_seconds": 3.0})
    thread.start()
    time.sleep(1.2)
    service.apply_settings(monitoring=True)  # resume mid-run via the API path
    thread.join(timeout=15)
    assert not thread.is_alive()

    assert any(p.get("state") == "MONITORING_OFF" and p.get("monitoring") is False for p in published)
    assert service.latest_status["monitoring"] is True  # resumed and processing
    assert service.latest_status["stream_status"] == "connected"  # source restarted on resume
    assert SettingsStore(tmp_path).get().monitoring_enabled is True


def test_paused_service_never_starts_the_stream(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.update(rtsp_url="demo://breathing", monitoring_enabled=False)
    service = BabyRespirationService(
        AppConfig(mqtt=MQTTConfig(enabled=False), debug=DebugConfig(enabled=False)),
        settings_store=store,
    )
    thread = threading.Thread(target=service.run, kwargs={"run_seconds": 2.0})
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    # the decoder thread must never have been spun up while paused
    assert service.source._thread is None
    assert service.latest_status["state"] == "MONITORING_OFF"


def test_tuning_overrides_apply_live(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.apply_settings(tuning={"low_rate_threshold_bpm": 16, "minimum_confidence": 50, "presence_enabled": False})
    service._apply_pending()
    assert service._signal.low_rate_threshold_bpm == 16
    assert service.classifier.config is service._signal
    assert service.estimator.config is service._signal
    assert service.presence.enabled is False
    # persisted for next start
    assert SettingsStore(tmp_path).get().tuning["minimum_confidence"] == 50

    import pytest as _pytest
    with _pytest.raises(ValueError):
        service.apply_settings(tuning={"max_bpm": 300})
    with _pytest.raises(ValueError):
        service.apply_settings(tuning={"bogus_field": 1})

    service.apply_settings(tuning={})  # reset to defaults
    service._apply_pending()
    assert service._signal.low_rate_threshold_bpm == service.config.signal.low_rate_threshold_bpm

    # camera tuning rebuilds the pipeline live
    service.apply_settings(tuning={"processing_fps": 4, "target_processing_width": 256})
    service._apply_pending()
    assert service._camera.processing_fps == 4
    assert service.extractor.camera.target_processing_width == 256
    with _pytest.raises(ValueError):  # Nyquist: max_bpm 120 needs fps > 4
        service.apply_settings(tuning={"processing_fps": 4, "max_bpm": 120})

    # MQTT topic override flows into the effective broker config
    service.apply_settings(mqtt={"base_topic": "nursery"})
    assert service._effective_mqtt().base_topic == "nursery"
    service.apply_settings(mqtt={"base_topic": ""})
    assert service._effective_mqtt().base_topic == "baby_respiration"


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
