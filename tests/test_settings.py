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


def test_corrupt_or_invalid_content_starts_unconfigured(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert SettingsStore(tmp_path).get().rtsp_url == ""

    (tmp_path / "settings.json").write_text(
        json.dumps({"rtsp_url": "rtsp://cam/stream", "roi": [2, 2, -1, 0]}), encoding="utf-8"
    )
    settings = SettingsStore(tmp_path).get()
    assert settings.rtsp_url == "rtsp://cam/stream"
    assert settings.roi is None
