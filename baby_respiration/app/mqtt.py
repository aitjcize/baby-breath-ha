from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

from app import __version__
from app.config import MQTTConfig

LOGGER = logging.getLogger(__name__)
DEVICE_ID = "baby_respiration_detector"


def _device() -> dict[str, Any]:
    return {
        "identifiers": [DEVICE_ID],
        "name": "Baby Respiration Detector",
        "manufacturer": "Local experimental monitor",
        "model": "Video optical-flow baseline",
        "sw_version": __version__,
    }


def build_discovery_payloads(config: MQTTConfig) -> dict[str, dict[str, Any]]:
    base = config.base_topic.rstrip("/")
    discovery = config.discovery_prefix.rstrip("/")
    state_topic = f"{base}/state"
    service_availability = f"{base}/availability"
    measurement_availability = f"{base}/measurement_availability"
    presence_availability = f"{base}/presence_availability"

    def common(name: str, object_id: str, availability: str = service_availability) -> dict[str, Any]:
        return {
            "name": name,
            "object_id": object_id,
            "unique_id": object_id,
            "state_topic": state_topic,
            "availability_topic": availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": _device(),
            "origin": {"name": "baby-breath-ha", "sw_version": __version__},
        }

    entities: list[tuple[str, str, dict[str, Any]]] = []
    rate = common("Respiration rate", "baby_respiration_rate", measurement_availability)
    rate.update({"value_template": "{{ value_json.bpm }}", "unit_of_measurement": "breaths/min", "state_class": "measurement", "icon": "mdi:lungs"})
    entities.append(("sensor", "baby_respiration_rate", rate))

    confidence = common("Respiration confidence", "baby_respiration_confidence")
    confidence.update({"value_template": "{{ value_json.confidence }}", "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:signal", "entity_category": "diagnostic"})
    entities.append(("sensor", "baby_respiration_confidence", confidence))

    breathing = common("Breathing detected", "baby_breathing_detected", measurement_availability)
    breathing.update({"value_template": "{{ 'ON' if value_json.breathing_detected else 'OFF' }}", "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:lungs"})
    entities.append(("binary_sensor", "baby_breathing_detected", breathing))

    valid = common("Respiration measurement valid", "baby_respiration_measurement_valid")
    valid.update({"value_template": "{{ 'ON' if value_json.measurement_valid else 'OFF' }}", "payload_on": "ON", "payload_off": "OFF", "entity_category": "diagnostic", "icon": "mdi:check-decagram-outline"})
    entities.append(("binary_sensor", "baby_respiration_measurement_valid", valid))

    presence = common("Baby presence", "baby_presence")
    presence.update({"value_template": "{{ value_json.presence }}", "icon": "mdi:crib"})
    entities.append(("sensor", "baby_presence", presence))

    in_crib = common("Baby in crib", "baby_in_crib", presence_availability)
    in_crib.update({
        "value_template": "{{ 'ON' if value_json.presence == 'PRESENT' else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "occupancy",
        "icon": "mdi:crib",
    })
    entities.append(("binary_sensor", "baby_in_crib", in_crib))

    diagnostics = {
        "baby_respiration_state": ("Detector state", "state", None, "mdi:state-machine"),
        "baby_respiration_signal_rms": ("Signal RMS", "signal_rms", "px", "mdi:chart-bell-curve"),
        "baby_respiration_video_fps": ("Video FPS", "estimated_fps", "fps", "mdi:video"),
        "baby_respiration_analysis_window": ("Analysis window", "window_seconds", "s", "mdi:timeline-clock"),
        "baby_respiration_stream_status": ("Stream status", "stream_status", None, "mdi:cctv"),
        "baby_respiration_snr": ("Signal-to-noise ratio", "snr_db", "dB", "mdi:signal-distance-variant"),
        "baby_respiration_reason": ("Measurement reason", "reason", None, "mdi:information-outline"),
    }
    for object_id, (name, field, unit, icon) in diagnostics.items():
        payload = common(name, object_id)
        payload.update({"value_template": f"{{{{ value_json.{field} }}}}", "icon": icon, "entity_category": "diagnostic"})
        if unit:
            payload["unit_of_measurement"] = unit
        entities.append(("sensor", object_id, payload))

    excessive = common("Excessive motion", "baby_respiration_excessive_motion")
    excessive.update({"value_template": "{{ 'ON' if value_json.excessive_motion else 'OFF' }}", "payload_on": "ON", "payload_off": "OFF", "entity_category": "diagnostic", "icon": "mdi:motion-sensor"})
    entities.append(("binary_sensor", "baby_respiration_excessive_motion", excessive))

    return {f"{discovery}/{component}/{DEVICE_ID}/{object_id}/config": payload for component, object_id, payload in entities}


class MQTTPublisher:
    def __init__(self, config: MQTTConfig) -> None:
        self.config = config
        self._connected = False
        self._client: mqtt.Client | None = None

    def start(self) -> None:
        if not self.config.enabled:
            LOGGER.warning("MQTT publishing disabled in config")
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.config.client_id, protocol=mqtt.MQTTv311)
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password or None)
        if self.config.tls:
            client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.will_set(f"{self.config.base_topic}/availability", "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        self._client = client
        try:
            client.connect_async(self.config.host, self.config.port, keepalive=30)
            client.loop_start()
        except Exception as exc:
            LOGGER.error("unable to start MQTT client: %s", exc)

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        del userdata, flags, properties
        if reason_code.is_failure:
            LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        self._connected = True
        LOGGER.info("MQTT connected")
        client.publish(f"{self.config.base_topic}/availability", "online", qos=1, retain=True)
        for topic, payload in build_discovery_payloads(self.config).items():
            client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=True)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        del client, userdata, flags, properties
        self._connected = False
        if reason_code.is_failure:
            LOGGER.warning("unexpected MQTT disconnect: %s", reason_code)

    def publish(self, state: dict[str, Any]) -> None:
        if not self._client or not self._connected:
            return
        self._client.publish(f"{self.config.base_topic}/state", json.dumps(state, allow_nan=False, separators=(",", ":")), qos=0, retain=True)
        availability = "online" if state.get("measurement_valid") else "offline"
        self._client.publish(f"{self.config.base_topic}/measurement_availability", availability, qos=1, retain=True)
        # The in-crib binary sensor is only meaningful once presence is decided.
        presence_known = state.get("presence") in ("PRESENT", "ABSENT")
        self._client.publish(
            f"{self.config.base_topic}/presence_availability",
            "online" if presence_known else "offline",
            qos=1,
            retain=True,
        )

    def stop(self) -> None:
        if not self._client:
            return
        if self._connected:
            self._client.publish(f"{self.config.base_topic}/measurement_availability", "offline", qos=1, retain=True)
            self._client.publish(f"{self.config.base_topic}/presence_availability", "offline", qos=1, retain=True)
            self._client.publish(f"{self.config.base_topic}/availability", "offline", qos=1, retain=True)
            self._client.disconnect()
        self._client.loop_stop()
        self._client = None
        self._connected = False
