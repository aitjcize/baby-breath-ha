from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from app.web import WebServer, WebState


class FakeController:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []
        self.scan_started = 0

    def probe(self, url: str) -> dict[str, Any]:
        return {"ok": url.startswith("rtsp://good"), "message": "test"}

    def probe_preview(self) -> bytes | None:
        return b"\xff\xd8fakejpeg"

    def apply_settings(self, rtsp_url: str | None = None, roi: Any = None, mqtt: Any = None) -> dict[str, Any]:
        self.applied.append({"rtsp_url": rtsp_url, "roi": roi, "mqtt": mqtt})
        return {"applied": True}

    def start_scan(self) -> tuple[bool, str]:
        self.scan_started += 1
        return True, "started"

    def cancel_scan(self) -> None:
        pass


@pytest.fixture()
def server():
    state = WebState((0.25, 0.35, 0.5, 0.35))
    controller = FakeController()
    web = WebServer("127.0.0.1", 0, state, controller)
    web.start()
    host, port = web.address
    yield f"http://{host}:{port}", controller, state
    web.stop()


def request(url: str, method: str = "GET", body: dict | None = None, csrf: bool = True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if csrf and method == "POST":
        req.add_header("X-Requested-With", "XMLHttpRequest")
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.status, json.loads(response.read() or b"{}")


def test_index_and_status(server) -> None:
    base, _, _ = server
    with urllib.request.urlopen(base + "/", timeout=5) as response:
        assert response.status == 200
        assert b"Baby Respiration" in response.read()
    status, payload = request(base + "/api/status")
    assert status == 200
    assert payload["camera_configured"] is False
    assert payload["scan"]["state"] == "idle"


def test_post_without_csrf_header_is_rejected(server) -> None:
    base, controller, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(base + "/api/settings", "POST", {"rtsp_url": "rtsp://x"}, csrf=False)
    assert exc.value.code == 403
    assert controller.applied == []


def test_probe_settings_and_scan(server) -> None:
    base, controller, _ = server
    _, payload = request(base + "/api/probe", "POST", {"url": "rtsp://good/stream"})
    assert payload["ok"] is True

    _, payload = request(base + "/api/settings", "POST", {"rtsp_url": "rtsp://good/stream", "roi": [0.1, 0.2, 0.3, 0.4]})
    assert payload["applied"] is True
    assert controller.applied == [{"rtsp_url": "rtsp://good/stream", "roi": [0.1, 0.2, 0.3, 0.4], "mqtt": None}]

    _, payload = request(base + "/api/scan/start", "POST")
    assert payload["ok"] is True
    assert controller.scan_started == 1

    with urllib.request.urlopen(base + "/probe-preview.jpg", timeout=5) as response:
        assert response.read().startswith(b"\xff\xd8")
