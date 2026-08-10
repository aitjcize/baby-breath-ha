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
        "baby_breathing_rate_low",
        "baby_presence",
        "baby_respiration_state",
    }
    object_ids = {payload["object_id"] for payload in payloads.values()}
    assert required <= object_ids
    assert all(topic.startswith("homeassistant/") and topic.endswith("/config") for topic in encoded)
    assert all(payload["state_topic"] == "baby_respiration/state" for payload in payloads.values())
    assert all(payload["device"]["identifiers"] == ["baby_respiration_detector"] for payload in payloads.values())

    removed = {"baby_breathing_detected", "baby_respiration_measurement_valid", "baby_in_crib"}
    assert not removed & object_ids  # derived binaries consolidated into the enums

    presence = next(payload for payload in payloads.values() if payload["object_id"] == "baby_presence")
    assert presence["icon"] == "mdi:teddy-bear"

    switch = next(payload for payload in payloads.values() if payload["object_id"] == "baby_monitoring")
    assert switch["command_topic"] == "baby_respiration/monitoring/set"

    state = next(payload for payload in payloads.values() if payload["object_id"] == "baby_respiration_state")
    assert "entity_category" not in state  # primary, not diagnostic
    assert state["device_class"] == "enum"
    assert "MONITORING_OFF" in state["options"]

    snr = next(payload for payload in payloads.values() if payload["object_id"] == "baby_respiration_snr")
    assert snr["entity_category"] == "diagnostic"
    assert snr["state_class"] == "measurement"


def test_availability_publishes_before_state_when_going_offline() -> None:
    from app.mqtt import MQTTPublisher

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def publish(self, topic, payload=None, qos=0, retain=False):
            self.calls.append(topic)

    publisher = MQTTPublisher(MQTTConfig())
    fake = FakeClient()
    publisher._client = fake  # type: ignore[assignment]
    publisher._connected = True

    publisher.publish({"measurement_valid": False, "presence": "UNKNOWN"})
    assert fake.calls.index("baby_respiration/measurement_availability") < fake.calls.index("baby_respiration/state")
    assert "baby_respiration/presence_availability" not in fake.calls

    fake.calls.clear()
    publisher.publish({"measurement_valid": True, "presence": "PRESENT"})
    assert fake.calls.index("baby_respiration/state") < fake.calls.index("baby_respiration/measurement_availability")


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

