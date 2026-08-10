"""Persisted runtime settings managed through the onboarding/setup web UI.

These settings (camera URL, ROI, MQTT broker choice) take precedence over the
static configuration because the user entered them interactively. They live in
a small JSON file inside the data directory: ``/data`` under the Home
Assistant add-on, or ``./data`` (overridable with ``BABY_DATA_DIR``)
elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from app.config import normalize_roi

LOGGER = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"

MQTT_MODES = ("auto", "custom", "disabled")


def default_data_dir() -> Path:
    env_dir = os.environ.get("BABY_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    if Path("/data").is_dir():  # Home Assistant add-on persistent storage
        return Path("/data")
    return Path("data")


@dataclass(frozen=True)
class RuntimeSettings:
    rtsp_url: str = ""
    roi: tuple[float, float, float, float] | None = None
    # "auto": broker provided by Home Assistant (or the static config in
    # standalone mode); "custom": the fields below; "disabled": no MQTT.
    mqtt_mode: str = "auto"
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    monitoring_enabled: bool = True
    # Panel-set detection tuning; keys mirror SignalConfig fields and
    # override the add-on option defaults. Empty dict = all defaults.
    tuning: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.tuning is None:
            object.__setattr__(self, "tuning", {})


class SettingsStore:
    """Thread-safe load/save of user-entered settings.

    A store that cannot persist (read-only filesystem) still works in memory
    so the wizard remains usable; it just warns that values will not survive
    a restart.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self._dir = data_dir if data_dir is not None else default_data_dir()
        self._path = self._dir / SETTINGS_FILENAME
        self._lock = threading.Lock()
        self._settings = self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data_dir(self) -> Path:
        return self._dir

    def _load(self) -> RuntimeSettings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return RuntimeSettings()
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("could not read %s (%s); starting unconfigured", self._path, exc)
            return RuntimeSettings()
        if not isinstance(raw, dict):
            LOGGER.warning("%s does not contain an object; starting unconfigured", self._path)
            return RuntimeSettings()
        roi_raw = raw.get("roi")
        roi: tuple[float, float, float, float] | None = None
        if roi_raw is not None:
            try:
                roi = normalize_roi(roi_raw)
            except ValueError as exc:
                LOGGER.warning("ignoring invalid stored ROI: %s", exc)
        mode = str(raw.get("mqtt_mode", "auto"))
        if mode not in MQTT_MODES:
            mode = "auto"
        try:
            port = int(raw.get("mqtt_port", 1883))
        except (TypeError, ValueError):
            port = 1883
        return RuntimeSettings(
            rtsp_url=str(raw.get("rtsp_url", "") or ""),
            roi=roi,
            mqtt_mode=mode,
            mqtt_host=str(raw.get("mqtt_host", "") or ""),
            mqtt_port=port,
            mqtt_username=str(raw.get("mqtt_username", "") or ""),
            mqtt_password=str(raw.get("mqtt_password", "") or ""),
            monitoring_enabled=bool(raw.get("monitoring_enabled", True)),
            tuning=dict(raw.get("tuning") or {}),
        )

    def get(self) -> RuntimeSettings:
        with self._lock:
            return self._settings

    def update(
        self,
        *,
        rtsp_url: str | None = None,
        roi: tuple[float, float, float, float] | None = None,
        mqtt_mode: str | None = None,
        mqtt_host: str | None = None,
        mqtt_port: int | None = None,
        mqtt_username: str | None = None,
        mqtt_password: str | None = None,
        monitoring_enabled: bool | None = None,
        tuning: dict | None = None,
    ) -> RuntimeSettings:
        """Merge the given fields into the stored settings and persist."""
        if mqtt_mode is not None and mqtt_mode not in MQTT_MODES:
            raise ValueError(f"mqtt_mode must be one of {MQTT_MODES}")
        if mqtt_port is not None and not 1 <= int(mqtt_port) <= 65535:
            raise ValueError("mqtt_port is invalid")
        with self._lock:
            merged = self._settings
            if rtsp_url is not None:
                merged = replace(merged, rtsp_url=rtsp_url.strip())
            if roi is not None:
                merged = replace(merged, roi=normalize_roi(roi))
            if mqtt_mode is not None:
                merged = replace(merged, mqtt_mode=mqtt_mode)
            if mqtt_host is not None:
                merged = replace(merged, mqtt_host=mqtt_host.strip())
            if mqtt_port is not None:
                merged = replace(merged, mqtt_port=int(mqtt_port))
            if mqtt_username is not None:
                merged = replace(merged, mqtt_username=mqtt_username)
            if mqtt_password is not None:
                merged = replace(merged, mqtt_password=mqtt_password)
            if monitoring_enabled is not None:
                merged = replace(merged, monitoring_enabled=bool(monitoring_enabled))
            if tuning is not None:
                merged = replace(merged, tuning=dict(tuning))
            self._settings = merged
            self._persist(merged)
            return merged

    def _persist(self, settings: RuntimeSettings) -> None:
        payload: dict[str, object] = {
            "rtsp_url": settings.rtsp_url,
            "mqtt_mode": settings.mqtt_mode,
            "mqtt_host": settings.mqtt_host,
            "mqtt_port": settings.mqtt_port,
            "mqtt_username": settings.mqtt_username,
            "mqtt_password": settings.mqtt_password,
            "monitoring_enabled": settings.monitoring_enabled,
            "tuning": settings.tuning,
        }
        if settings.roi is not None:
            payload["roi"] = list(settings.roi)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, self._path)
        except OSError as exc:
            LOGGER.warning("settings kept in memory only; could not write %s: %s", self._path, exc)
