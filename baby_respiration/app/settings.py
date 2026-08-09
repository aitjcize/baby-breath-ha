"""Persisted runtime settings managed through the onboarding/setup web UI.

These settings (camera URL and ROI) take precedence over the static
configuration because the user entered them interactively. They live in a
small JSON file inside the data directory: ``/data`` under the Home Assistant
add-on, or ``./data`` (overridable with ``BABY_DATA_DIR``) elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import normalize_roi

LOGGER = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"


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
        url = raw.get("rtsp_url", "")
        roi_raw = raw.get("roi")
        roi: tuple[float, float, float, float] | None = None
        if roi_raw is not None:
            try:
                roi = normalize_roi(roi_raw)
            except ValueError as exc:
                LOGGER.warning("ignoring invalid stored ROI: %s", exc)
        return RuntimeSettings(rtsp_url=str(url or ""), roi=roi)

    def get(self) -> RuntimeSettings:
        with self._lock:
            return self._settings

    def update(
        self,
        *,
        rtsp_url: str | None = None,
        roi: tuple[float, float, float, float] | None = None,
    ) -> RuntimeSettings:
        """Merge the given fields into the stored settings and persist."""
        with self._lock:
            current = self._settings
            merged = RuntimeSettings(
                rtsp_url=current.rtsp_url if rtsp_url is None else rtsp_url.strip(),
                roi=current.roi if roi is None else normalize_roi(roi),
            )
            self._settings = merged
            self._persist(merged)
            return merged

    def _persist(self, settings: RuntimeSettings) -> None:
        payload = {"rtsp_url": settings.rtsp_url}
        if settings.roi is not None:
            payload["roi"] = list(settings.roi)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, self._path)
        except OSError as exc:
            LOGGER.warning("settings kept in memory only; could not write %s: %s", self._path, exc)
