"""Web UI server: onboarding wizard, live dashboard, and the JSON API.

Serves a single static page from ``static/index.html`` plus a small JSON API.
Designed to run behind Home Assistant ingress (all URLs are relative) or bound
to loopback in standalone mode. POST endpoints require the
``X-Requested-With`` header, which forces a CORS preflight for any cross-origin
caller and therefore blocks cross-site request forgery without cookies/tokens.
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 8192


class Controller(Protocol):
    """The service surface the web UI drives (implemented by BabyRespirationService)."""

    def probe(self, url: str) -> dict[str, Any]: ...

    def probe_preview(self) -> bytes | None: ...

    def apply_settings(self, rtsp_url: str | None = None, roi: Any = None) -> dict[str, Any]: ...

    def start_scan(self) -> tuple[bool, str]: ...

    def cancel_scan(self) -> None: ...


class WebState:
    """Thread-safe holder for the latest status payload and annotated frame."""

    def __init__(self, roi: tuple[float, float, float, float]) -> None:
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {
            "state": "MEASUREMENT_INVALID",
            "measurement_valid": False,
            "reason": "service_starting",
            "camera_configured": False,
            "waveform_t": [],
            "waveform_y": [],
            "roi": list(roi),
            "scan": {"state": "idle", "progress": 0},
        }
        self._jpeg: bytes | None = None

    def update(self, status: dict[str, Any], jpeg: bytes | None = None) -> None:
        with self._lock:
            self._status = dict(status)
            if jpeg is not None:
                self._jpeg = jpeg

    def snapshot(self) -> tuple[dict[str, Any], bytes | None]:
        with self._lock:
            return dict(self._status), self._jpeg

    def set_roi(self, roi: tuple[float, float, float, float]) -> None:
        with self._lock:
            self._status["roi"] = list(roi)
            self._status["state"] = "MEASUREMENT_INVALID"
            self._status["measurement_valid"] = False
            self._status["breathing_detected"] = None
            self._status["reason"] = "roi_changed_recalibrating"


def _load_index() -> bytes:
    return (STATIC_DIR / "index.html").read_bytes()


def _handler(state: WebState, controller: Controller | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            path = urlsplit(self.path).path
            status, jpeg = state.snapshot()
            if path == "/":
                try:
                    self._send(_load_index(), "text/html; charset=utf-8")
                except OSError as exc:
                    LOGGER.error("could not read index.html: %s", exc)
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            elif path == "/api/status":
                self._send_json(status)
            elif path == "/frame.jpg":
                if jpeg is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "No camera frame available")
                else:
                    self._send(jpeg, "image/jpeg")
            elif path == "/probe-preview.jpg":
                preview = controller.probe_preview() if controller else None
                if preview is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "No preview available")
                else:
                    self._send(preview, "image/jpeg")
            elif path == "/healthz":
                self._send(b'{"status":"running"}', "application/json")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 - stdlib API
            path = urlsplit(self.path).path
            if controller is None:
                self._send_json({"error": "service unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if self.headers.get("X-Requested-With") != "XMLHttpRequest":
                self._send_json({"error": "missing X-Requested-With header"}, HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._read_json_body() if path in ("/api/probe", "/api/settings") else {}
                if path == "/api/probe":
                    url = str(payload.get("url", ""))
                    self._send_json(controller.probe(url))
                elif path == "/api/settings":
                    result = controller.apply_settings(
                        rtsp_url=payload.get("rtsp_url"),
                        roi=payload.get("roi"),
                    )
                    self._send_json(result)
                elif path == "/api/scan/start":
                    ok, message = controller.start_scan()
                    self._send_json({"ok": ok, "message": message}, HTTPStatus.OK if ok else HTTPStatus.CONFLICT)
                elif path == "/api/scan/cancel":
                    controller.cancel_scan()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except OSError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _read_json_body(self) -> dict[str, Any]:
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(json.dumps(payload, allow_nan=False).encode(), "application/json", status)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("web HTTP: " + format, *args)

    return Handler


class WebServer:
    def __init__(self, host: str, port: int, state: WebState, controller: Controller | None = None) -> None:
        self._server = ThreadingHTTPServer((host, port), _handler(state, controller))
        self._thread = threading.Thread(target=self._server.serve_forever, name="web-http", daemon=True)

    def start(self) -> None:
        self._thread.start()
        LOGGER.info("web UI listening on http://%s:%d", *self._server.server_address[:2])

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)
