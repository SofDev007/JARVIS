"""The dashboard HTTP server: stdlib only, loopback by default.

Security posture, stated plainly:

* **Binding**: ``127.0.0.1`` unless ``dashboard.allow_remote`` is set —
  and config validation refuses a non-loopback host without that flag.
* **CSRF containment**: every state-changing endpoint requires the
  ``X-Dashboard-Token`` header. The token is random per server start and
  is embedded only in the served page — a malicious website open in the
  same browser can *send* a cross-origin POST at localhost, but cannot
  read the page to learn the token, so it cannot approve actions or
  inject chat.
* **Read endpoints** carry status/events only; browsers block
  cross-origin *reads* without CORS headers (which are never sent).

Endpoints::

    GET  /                      the dashboard page (token embedded)
    GET  /api/status            modules, bus stats, dispatcher metrics
    GET  /api/events            recent events (ring buffer)
    GET  /api/confirmations     pending approvals (web provider only)
    POST /api/confirmations/<id>  {"approve": true|false}     [token]
    POST /api/chat                {"text": "..."}             [token]
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Digital Twin — Dashboard</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--text:#d7dce4;
--dim:#8a93a3;--accent:#5aa9e6;--ok:#4caf7d;--warn:#e6b455;--bad:#e05d5d}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
font:14px/1.5 "Segoe UI",system-ui,sans-serif;padding:24px}
h1{font-size:18px;font-weight:600;margin-bottom:2px}
h1 span{color:var(--accent)} .sub{color:var(--dim);margin-bottom:20px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
color:var(--dim);margin-bottom:10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);
border-radius:10px;padding:16px;overflow:auto;max-height:420px}
.wide{grid-column:1/-1}
table{width:100%;border-collapse:collapse}
td,th{padding:4px 8px;text-align:left;border-bottom:1px solid var(--line);
font-size:13px} th{color:var(--dim);font-weight:500}
.state-running{color:var(--ok)} .state-paused{color:var(--warn)}
.state-failed,.state-stopped{color:var(--bad)}
.evt{font-family:ui-monospace,monospace;font-size:12px;padding:3px 0;
border-bottom:1px solid var(--line);color:var(--dim)}
.evt b{color:var(--text);font-weight:500}
.confirm{border:1px solid var(--warn);border-radius:8px;padding:10px;
margin-bottom:8px}
.confirm code{color:var(--warn)}
button{background:var(--accent);border:0;border-radius:6px;color:#0f1115;
padding:6px 14px;font-weight:600;cursor:pointer;margin-right:8px}
button.deny{background:var(--bad);color:#fff}
input{background:var(--bg);border:1px solid var(--line);border-radius:6px;
color:var(--text);padding:8px;width:100%}
.chatrow{display:flex;gap:8px;margin-top:8px} .chatrow input{flex:1}
.empty{color:var(--dim);font-style:italic}
.stats{display:flex;gap:18px;color:var(--dim);font-size:12px;
margin-top:8px;flex-wrap:wrap} .stats b{color:var(--text)}
</style></head><body>
<h1>Digital <span>Twin</span> — live dashboard</h1>
<div class="sub">v__VERSION__ · polling every second · loopback only</div>
<div class="grid">
 <div class="panel wide" id="confirm-panel" style="display:none">
  <h2>Waiting for your approval</h2><div id="confirms"></div></div>
 <div class="panel"><h2>Modules</h2>
  <table><thead><tr><th>module</th><th>state</th><th>detail</th></tr></thead>
  <tbody id="modules"></tbody></table>
  <div class="stats" id="busstats"></div></div>
 <div class="panel"><h2>Chat</h2>
  <div id="chatlog" style="max-height:300px;overflow:auto"></div>
  <div class="chatrow"><input id="chattext"
    placeholder="Type to the assistant…"><button id="send">Send</button></div>
 </div>
 <div class="panel wide"><h2>Recent events</h2><div id="events"></div></div>
 <div class="panel"><h2>Memory</h2><div id="memory"></div></div>
 <div class="panel"><h2>Knowledge</h2><div id="knowledge"></div></div>
 <div class="panel"><h2>Live view</h2><div id="live"></div>
  <div id="frames"></div></div>
 <div class="panel"><h2>Plugins</h2><div id="plugins"></div></div>
 <div class="panel wide"><h2>Settings (read-only — edit YAML + restart)</h2>
  <div id="settings"></div></div>
</div>
<script>
const TOKEN = "__TOKEN__";
const headers = {"Content-Type":"application/json",
                 "X-Dashboard-Token": TOKEN};
async function get(path){const r=await fetch(path);return r.json()}
async function post(path,body){return fetch(path,{method:"POST",headers,
  body:JSON.stringify(body)})}
function esc(s){const d=document.createElement("div");
  d.textContent=String(s);return d.innerHTML}
async function refresh(){
 try{
  const s=await get("/api/status");
  document.getElementById("modules").innerHTML=s.modules.map(m=>
   `<tr><td>${esc(m.name)}</td><td class="state-${esc(m.state)}">`+
   `${esc(m.state)}</td><td>${esc(m.detail||"")}</td></tr>`).join("");
  const b=s.bus;
  document.getElementById("busstats").innerHTML=
   `<span>published <b>${b.published}</b></span>`+
   `<span>delivered <b>${b.delivered}</b></span>`+
   `<span>dropped <b>${b.dropped}</b></span>`+
   `<span>handler errors <b>${b.handler_errors}</b></span>`;
  const e=await get("/api/events");
  document.getElementById("events").innerHTML=e.events.length?
   e.events.map(v=>`<div class="evt"><b>${esc(v.topic)}</b> ← `+
   `${esc(v.source)} ${esc(JSON.stringify(v.payload))}</div>`).join(""):
   '<div class="empty">nothing yet — wave, chat, or run a plan</div>';
  const chat=e.events.filter(v=>v.topic==="perception.chat"
    ||v.topic==="chat.response");
  document.getElementById("chatlog").innerHTML=chat.map(v=>
   v.topic==="perception.chat"
    ?`<div class="evt"><b>you</b> ${esc(v.payload.text||"")}</div>`
    :`<div class="evt"><b style="color:var(--accent)">twin</b> `+
      `${esc(v.payload.text||"")}</div>`).join("");
  const c=await get("/api/confirmations");
  const panel=document.getElementById("confirm-panel");
  if(c.pending.length){panel.style.display="block";
   document.getElementById("confirms").innerHTML=c.pending.map(p=>
    `<div class="confirm">Allow <code>${esc(p.action)}</code> `+
    `${esc(JSON.stringify(p.params))} <span class="empty">`+
    `(${p.expires_in_s}s left)</span><br><br>`+
    `<button onclick="answer('${p.id}',true)">Approve</button>`+
    `<button class="deny" onclick="answer('${p.id}',false)">Deny</button>`+
    `</div>`).join("");
  } else panel.style.display="none";
  try{
   const m=await get("/api/memory");
   const md=document.getElementById("memory");
   if(md)md.innerHTML=(m.entries&&m.entries.length)?m.entries.map(e=>
    `<div class="evt"><b>${esc(e.kind)}</b> ${esc(e.content)}</div>`).join("")
    :'<div class="empty">'+esc((m&&m.error)||"no memories yet")+'</div>';
  }catch(e){}
  try{
   const live=document.getElementById("live");
   if(live){
    const voice=s.modules.find(m=>m.name==="voice");
    const wake=s.modules.find(m=>m.name==="wake_word");
    const gest=s.modules.find(m=>m.name==="gesture");
    const badge=(label,on,detail)=>`<span class="evt" style="margin-right:14px">`+
     `<b style="color:${on?"var(--ok)":"var(--dim)"}">●</b> ${label}`+
     `${detail?` <span class="empty">${esc(detail)}</span>`:""}</span>`;
    live.innerHTML=
     badge("mic",voice&&voice.metrics&&voice.metrics.listening,
           voice?(voice.metrics.listening?"listening":"idle"):"off")+
     badge("wake",wake&&wake.state==="running",
           wake?`${wake.metrics.detections||0} detections`:"off")+
     badge("camera",gest&&gest.state==="running",
           gest?gest.state:"off");
   }
   const fr=await get("/api/frames").catch(()=>({sources:[]}));
   const fd=document.getElementById("frames");
   if(fd&&fr.sources&&fr.sources.length&&!fd.dataset.bound){
    fd.dataset.bound="1";
    fd.innerHTML=fr.sources.map(n=>
     `<img src="/api/frames/${esc(n)}" style="width:100%;border-radius:8px;`+
     `margin-top:8px" alt="${esc(n)}">`).join("");
   }
  }catch(e){}
  try{
   const pl=await get("/api/plugins");
   const pd=document.getElementById("plugins");
   if(pd)pd.innerHTML=(pl.plugins&&pl.plugins.length)?pl.plugins.map(p=>
    `<div class="evt"><b style="color:${p.ok?"var(--ok)":"var(--bad)"}">`+
    `${p.ok?"●":"✕"}</b> ${esc(p.name)} ${esc(p.version)}`+
    `${p.sandboxed?' <span class="empty">[sandboxed]</span>':""}`+
    `${p.ok?` <span class="empty">${p.actions.length} action(s)</span>`
           :` <span class="empty">${esc((p.error||"").slice(0,70))}</span>`}`+
    `</div>`).join("")
    :'<div class="empty">no plugins configured</div>';
  }catch(e){}
  try{
   const st=await get("/api/settings");
   const sd=document.getElementById("settings");
   if(sd&&st.sections){
    let rows="";
    for(const [section,fields] of Object.entries(st.sections)){
     for(const [key,info] of Object.entries(fields)){
      if(info&&info.default===false)
       rows+=`<div class="evt"><b>${esc(section)}.${esc(key)}</b> = `+
             `${esc(JSON.stringify(info.value))}</div>`;
     }
    }
    sd.innerHTML=rows||'<div class="empty">everything at defaults</div>';
   }
  }catch(e){}
  try{
   const k=await get("/api/knowledge");
   const kd=document.getElementById("knowledge");
   if(kd)kd.innerHTML=(k.documents&&k.documents.length)?
    `<div class="empty">embedder: ${esc(k.embedder||"?")}</div>`+
    k.documents.map(d=>`<div class="evt"><b>#${d.id}</b> ${esc(d.title)} `+
    `<span class="empty">(${d.chunks} chunks)</span></div>`).join("")
    :'<div class="empty">'+esc((k&&k.error)||"no documents ingested")+'</div>';
  }catch(e){}
 }catch(err){/* server restarting; keep polling */}
}
async function answer(id,ok){await post("/api/confirmations/"+id,
  {approve:ok});refresh()}
document.getElementById("send").onclick=async()=>{
 const box=document.getElementById("chattext");
 if(box.value.trim()){await post("/api/chat",{text:box.value.trim()});
  box.value="";setTimeout(refresh,300)}};
document.getElementById("chattext").addEventListener("keydown",
 e=>{if(e.key==="Enter")document.getElementById("send").click()});
let es=null;
try{
 es=new EventSource("/api/stream");
 es.onmessage=()=>refresh();          // push-driven updates
 setInterval(refresh,5000);           // slow fallback + panel refresh
}catch(e){setInterval(refresh,1000)}  // no SSE: poll as before
refresh();
</script></body></html>
"""


class DashboardServer:
    """Owns the ThreadingHTTPServer and its data callbacks."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        version: str,
        status_source: Callable[[], dict[str, Any]],
        events_source: Callable[[], list[dict[str, Any]]],
        chat_sink: Callable[[str], None],
        confirmations,  # WebConfirmation | None
        data_sources: "dict[str, Callable[[], Any]] | None" = None,
        stream_source: "Callable[[float], list[dict[str, Any]]] | None" = None,
        frame_hub=None,  # FrameHub | None — MJPEG at /api/frames/<name>
    ):
        self._token = _secrets.token_hex(16)
        page = _PAGE.replace("__TOKEN__", self._token).replace(
            "__VERSION__", version)
        sources = dict(data_sources or {})  # name -> callable, GET /api/<name>
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # route into our logging
                logger.debug("dashboard: " + fmt, *args)

            def _send(self, code: int, body: bytes,
                      content_type: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj).encode("utf-8"))

            def _authorized(self) -> bool:
                return self.headers.get("X-Dashboard-Token") == outer._token

            # -- GET ------------------------------------------------------
            def do_GET(self):
                if self.path == "/":
                    self._send(200, page.encode("utf-8"),
                               "text/html; charset=utf-8")
                elif self.path == "/api/status":
                    self._json(200, status_source())
                elif self.path == "/api/events":
                    self._json(200, {"events": events_source()})
                elif self.path == "/api/confirmations":
                    pending = (confirmations.pending()
                               if confirmations is not None else [])
                    self._json(200, {"pending": pending})
                elif self.path == "/api/stream" and stream_source is not None:
                    self._stream(stream_source)
                elif self.path == "/api/frames" and frame_hub is not None:
                    self._json(200, {"sources": list(frame_hub.sources())})
                elif self.path.startswith("/api/frames/") and \
                        frame_hub is not None:
                    self._mjpeg(frame_hub, self.path.rsplit("/", 1)[-1])
                elif self.path.startswith("/api/") and \
                        self.path[5:] in sources:
                    self._json(200, sources[self.path[5:]]())
                else:
                    self._json(404, {"error": "not found"})

            def _mjpeg(self, hub, name: str) -> None:
                """multipart/x-mixed-replace: forward frames as they arrive."""
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary=dtframe")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last_seq = 0
                try:
                    while not outer._closing:
                        result = hub.wait_next(name, last_seq, timeout_s=1.0)
                        if result is None:
                            continue  # producer quiet; keep waiting
                        jpeg, last_seq = result
                        self.wfile.write(
                            b"--dtframe\r\nContent-Type: image/jpeg\r\n"
                            + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                            + jpeg + b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # tab closed; normal

            def _stream(self, source) -> None:
                """Server-Sent Events: push new bus events as they arrive."""
                import time as _time

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                since = _time.time()
                try:
                    while not outer._closing:
                        batch = source(since)
                        if batch:
                            since = _time.time()
                            payload = json.dumps({"events": batch})
                            self.wfile.write(
                                f"data: {payload}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        else:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                        _time.sleep(1.0)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # client closed the tab; normal

            # -- POST (token required) -------------------------------------
            def do_POST(self):
                if not self._authorized():
                    self._json(403, {"error": "missing or bad token"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._json(400, {"error": "invalid JSON body"})
                    return
                if self.path.startswith("/api/confirmations/"):
                    if confirmations is None:
                        self._json(409, {"error":
                                         "web confirmations not enabled"})
                        return
                    confirmation_id = self.path.rsplit("/", 1)[-1]
                    resolved = confirmations.resolve(
                        confirmation_id, bool(body.get("approve")))
                    self._json(200 if resolved else 404,
                               {"resolved": resolved})
                elif self.path == "/api/chat":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        self._json(400, {"error": "empty text"})
                        return
                    chat_sink(text[:2000])
                    self._json(200, {"accepted": True})
                else:
                    self._json(404, {"error": "not found"})

        self._closing = False
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        """The actual bound port (useful with ``port: 0``)."""
        return self._server.server_address[1]

    @property
    def token(self) -> str:
        return self._token

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="dashboard-http", daemon=True,
        )
        self._thread.start()
        logger.info("Dashboard listening on http://%s:%s",
                    self._server.server_address[0], self.port)

    def stop(self) -> None:
        self._closing = True  # let SSE loops exit promptly
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
