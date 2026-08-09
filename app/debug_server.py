from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Baby respiration detector</title>
<style>
body{font:15px system-ui,sans-serif;background:#111827;color:#e5e7eb;margin:0;padding:20px}main{max-width:1050px;margin:auto}
h1{font-size:22px}.warning{background:#7f1d1d;padding:12px;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:16px 0}
.card{background:#1f2937;padding:12px;border-radius:8px}.label{color:#9ca3af;font-size:12px}.value{font-size:20px;margin-top:3px;overflow-wrap:anywhere}
.frame-wrap{position:relative;width:100%;background:#030712;border-radius:8px;overflow:hidden}.frame-wrap img{display:block;width:100%}#roiCanvas{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair;touch-action:none;background:transparent}
#wave{width:100%;height:220px;background:#030712;border-radius:8px}.empty{display:grid;place-items:center;min-height:180px;background:#030712;color:#9ca3af;border-radius:8px}
.editor{background:#1f2937;padding:14px;border-radius:8px;margin:12px 0 20px}.editor h2{margin:0 0 6px;font-size:18px}.hint{color:#9ca3af;margin:0 0 12px}.roi-fields{display:grid;grid-template-columns:repeat(4,minmax(90px,1fr));gap:10px}.roi-fields label{color:#9ca3af;font-size:12px}.roi-fields input{box-sizing:border-box;width:100%;margin-top:4px;padding:8px;border:1px solid #4b5563;border-radius:6px;background:#111827;color:#e5e7eb;font:inherit}.actions{display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap}button{border:0;border-radius:6px;padding:9px 14px;background:#0891b2;color:white;font-weight:650;cursor:pointer}button.secondary{background:#4b5563}button:disabled{opacity:.5;cursor:wait}.message{color:#93c5fd}.message.error{color:#fca5a5}code{color:#fbbf24}.invalid{color:#f87171}.valid{color:#4ade80}@media(max-width:600px){.roi-fields{grid-template-columns:repeat(2,1fr)}}
</style></head><body><main><h1>Baby respiration detector</h1>
<div class="warning"><strong>Experimental secondary monitor only.</strong> Not a medical device or a life-safety system. Never rely on it as the primary way to monitor an infant.</div>
<div class="grid" id="cards"></div><div id="frameEmpty" class="empty">No camera frame available — ROI coordinates can still be entered below</div>
<div id="frameWrap" class="frame-wrap" hidden><img id="frame" alt="Latest camera frame"><canvas id="roiCanvas" aria-label="Drag to select respiration region"></canvas></div>
<section class="editor"><h2>Respiration ROI</h2><p class="hint">Drag a rectangle on the camera frame, or enter normalized coordinates. Applying a new ROI resets the analysis window.</p>
<div class="roi-fields"><label>X<input id="roiX" type="number" min="0" max="1" step="0.001"></label><label>Y<input id="roiY" type="number" min="0" max="1" step="0.001"></label><label>Width<input id="roiW" type="number" min="0.01" max="1" step="0.001"></label><label>Height<input id="roiH" type="number" min="0.01" max="1" step="0.001"></label></div>
<div class="actions"><button id="applyRoi" class="secondary">Apply live</button><button id="saveRoi">Apply and save to config.yaml</button><span id="roiMessage" class="message" role="status"></span></div></section>
<h2>Filtered ROI motion waveform</h2><canvas id="wave"></canvas>
<p>The confidence score is signal quality, <strong>not</strong> a medical probability.</p>
<script>
const fields=[['state','State'],['bpm','BPM'],['confidence','Confidence %'],['measurement_valid','Valid'],['stream_status','Stream'],['estimated_fps','FPS'],['signal_rms','Signal RMS'],['snr_db','SNR dB'],['reason','Reason']];
let selectedRoi=null,dragStart=null,dragging=false,roiDirty=false;
const frame=document.getElementById('frame'),frameWrap=document.getElementById('frameWrap'),frameEmpty=document.getElementById('frameEmpty'),roiCanvas=document.getElementById('roiCanvas');
const roiInputs=['roiX','roiY','roiW','roiH'].map(id=>document.getElementById(id));
function drawWaveform(y){const c=document.getElementById('wave'),d=devicePixelRatio||1;c.width=c.clientWidth*d;c.height=220*d;const x=c.getContext('2d');x.scale(d,d);let w=c.clientWidth,h=220;x.strokeStyle='#374151';x.beginPath();x.moveTo(0,h/2);x.lineTo(w,h/2);x.stroke();if(!y||y.length<2)return;let lo=Math.min(...y),hi=Math.max(...y),span=Math.max(hi-lo,1e-9);x.strokeStyle='#22d3ee';x.lineWidth=2;x.beginPath();y.forEach((v,i)=>{let px=i*w/(y.length-1),py=h-10-(v-lo)*(h-20)/span;i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}
function setRoiFields(roi){roi.forEach((v,i)=>roiInputs[i].value=Number(v).toFixed(4))}
function readRoiFields(){return roiInputs.map(input=>Number(input.value))}
function drawRoi(){if(!selectedRoi||frameWrap.hidden)return;const r=frame.getBoundingClientRect(),d=devicePixelRatio||1;roiCanvas.width=Math.max(1,Math.round(r.width*d));roiCanvas.height=Math.max(1,Math.round(r.height*d));const x=roiCanvas.getContext('2d');x.scale(d,d);const [rx,ry,rw,rh]=selectedRoi;x.fillStyle='rgba(34,211,238,.18)';x.strokeStyle='#22d3ee';x.lineWidth=2;x.fillRect(rx*r.width,ry*r.height,rw*r.width,rh*r.height);x.strokeRect(rx*r.width,ry*r.height,rw*r.width,rh*r.height)}
function point(event){const r=roiCanvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(event.clientY-r.top)/r.height))]}
function updateDrag(event){const p=point(event),x=Math.min(dragStart[0],p[0]),y=Math.min(dragStart[1],p[1]);selectedRoi=[x,y,Math.abs(p[0]-dragStart[0]),Math.abs(p[1]-dragStart[1])];setRoiFields(selectedRoi);drawRoi()}
roiCanvas.addEventListener('pointerdown',event=>{dragging=true;roiDirty=true;dragStart=point(event);roiCanvas.setPointerCapture(event.pointerId);updateDrag(event)});
roiCanvas.addEventListener('pointermove',event=>{if(dragging)updateDrag(event)});
roiCanvas.addEventListener('pointerup',event=>{if(dragging)updateDrag(event);dragging=false;dragStart=null});
roiCanvas.addEventListener('pointercancel',()=>{dragging=false;dragStart=null});
roiInputs.forEach(input=>input.addEventListener('input',()=>{selectedRoi=readRoiFields();roiDirty=true;drawRoi()}));
window.addEventListener('resize',drawRoi);
async function submitRoi(persist){const message=document.getElementById('roiMessage'),buttons=[document.getElementById('applyRoi'),document.getElementById('saveRoi')];message.classList.remove('error');message.textContent='Applying…';buttons.forEach(button=>button.disabled=true);try{const response=await fetch('/api/roi',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({roi:readRoiFields(),persist})}),result=await response.json();if(!response.ok)throw new Error(result.error||'ROI update failed');selectedRoi=result.roi;setRoiFields(selectedRoi);roiDirty=false;drawRoi();message.textContent=persist?'Applied live and saved to config.yaml':'Applied live; not saved'}catch(error){message.classList.add('error');message.textContent=error.message}finally{buttons.forEach(button=>button.disabled=false)}}
document.getElementById('applyRoi').addEventListener('click',()=>submitRoi(false));document.getElementById('saveRoi').addEventListener('click',()=>submitRoi(true));
async function update(){try{let r=await fetch('/api/status',{cache:'no-store'}),s=await r.json();document.getElementById('cards').innerHTML=fields.map(([k,n])=>`<div class="card"><div class="label">${n}</div><div class="value ${k==='measurement_valid'?(s[k]?'valid':'invalid'):''}">${s[k]??'—'}</div></div>`).join('');drawWaveform(s.waveform_y);if(!roiDirty&&s.roi){selectedRoi=s.roi;setRoiFields(selectedRoi);drawRoi()}frame.onload=()=>{frameWrap.hidden=false;frameEmpty.hidden=true;drawRoi()};frame.onerror=()=>{frameWrap.hidden=true;frameEmpty.hidden=false};frame.src='/frame.jpg?t='+Date.now()}catch(e){console.error(e)}}
update();setInterval(update,1000);
</script></main></body></html>"""

ROIUpdateCallback = Callable[[tuple[float, float, float, float], bool], dict[str, Any]]


class DebugState:
    def __init__(self, roi: tuple[float, float, float, float]) -> None:
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {"state": "MEASUREMENT_INVALID", "measurement_valid": False, "reason": "service_starting", "waveform_t": [], "waveform_y": [], "roi": list(roi)}
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


def _handler(state: DebugState, roi_update: ROIUpdateCallback | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            status, jpeg = state.snapshot()
            if self.path == "/" or self.path.startswith("/?"):
                self._send(INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/status"):
                self._send(json.dumps(status, allow_nan=False).encode(), "application/json")
            elif self.path.startswith("/frame.jpg"):
                if jpeg is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "No camera frame available")
                else:
                    self._send(jpeg, "image/jpeg")
            elif self.path == "/healthz":
                self._send(b'{"status":"running"}', "application/json")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 - stdlib API
            if urlsplit(self.path).path != "/api/roi":
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                self._send_json({"error": "ROI updates require a loopback origin"}, HTTPStatus.FORBIDDEN)
                return
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._send_json({"error": "Content-Type must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 4096:
                self._send_json({"error": "invalid request size"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                payload = json.loads(self.rfile.read(length))
                roi = tuple(payload["roi"])
                persist = bool(payload.get("persist", False))
                if roi_update is None:
                    raise RuntimeError("ROI updates are unavailable")
                result = roi_update(roi, persist)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except (OSError, RuntimeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            self._send_json(result, HTTPStatus.OK)

        def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus) -> None:
            self._send(json.dumps(payload, allow_nan=False).encode(), "application/json", status)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("debug HTTP: " + format, *args)

    return Handler


class DebugServer:
    def __init__(self, host: str, port: int, state: DebugState, roi_update: ROIUpdateCallback | None = None) -> None:
        self._server = ThreadingHTTPServer((host, port), _handler(state, roi_update))
        self._thread = threading.Thread(target=self._server.serve_forever, name="debug-http", daemon=True)

    def start(self) -> None:
        self._thread.start()
        LOGGER.info("debug UI listening on http://%s:%d", *self._server.server_address[:2])

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)
