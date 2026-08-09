from __future__ import annotations

import json

from app.config import MQTTConfig
from app.mqtt import build_discovery_payloads


def test_discovery_payloads_are_valid_and_include_required_entities() -> None:
    payloads = build_discovery_payloads(MQTTConfig())
    encoded = {topic: json.dumps(payload, allow_nan=False) for topic, payload in payloads.items()}
    required = {
        "baby_respiration_rate",
        "baby_respiration_confidence",
        "baby_breathing_detected",
        "baby_respiration_measurement_valid",
        "baby_breathing_rate_low",
        "baby_presence",
        "baby_in_crib",
    }
    object_ids = {payload["object_id"] for payload in payloads.values()}
    assert required <= object_ids
    assert all(topic.startswith("homeassistant/") and topic.endswith("/config") for topic in encoded)
    assert all(payload["state_topic"] == "baby_respiration/state" for payload in payloads.values())
    assert all(payload["device"]["identifiers"] == ["baby_respiration_detector"] for payload in payloads.values())

    breathing = next(payload for payload in payloads.values() if payload["object_id"] == "baby_breathing_detected")
    assert breathing["availability_topic"] == "baby_respiration/measurement_availability"

    switch = next(payload for payload in payloads.values() if payload["object_id"] == "baby_monitoring")
    assert switch["command_topic"] == "baby_respiration/monitoring/set"


def test_monitoring_command_parsing() -> None:
    from types import SimpleNamespace

    from app.mqtt import MQTTPublisher

    received = []
    publisher = MQTTPublisher(MQTTConfig(), on_monitoring_command=received.append)
    topic = "baby_respiration/monitoring/set"
    publisher._on_message(None, None, SimpleNamespace(topic=topic, payload=b"ON"))
    publisher._on_message(None, None, SimpleNamespace(topic=topic, payload=b"off"))
    publisher._on_message(None, None, SimpleNamespace(topic=topic, payload=b"garbage"))
    publisher._on_message(None, None, SimpleNamespace(topic="other/topic", payload=b"ON"))
    assert received == [True, False]

