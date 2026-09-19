"""The air-board HTTP server: stdlib only, loopback by default.

Ported from the barehands reference implementation — same endpoints, same
security model, same wire shapes. See ``digital_twin/airboard/module.py``
for how this fits the kernel's module lifecycle.

Security posture, stated plainly (matches the reference: this is a
local-only tool, there is no token/auth layer):

* **Binding**: ``127.0.0.1`` unless ``airboard.allow_remote`` is set — and
  config validation refuses a non-loopback host without that flag.
* **Path jails**: every endpoint that touches a file (``/media/*``,
  ``/cmd``'s ``src``, ``/tree``, ``/note``, ``/props``) resolves the
  candidate path fully (``.resolve()``) and then proves containment via
  ``root in target.parents`` — never a string-prefix check, which is
  bypassable via ``..``, symlinks, or a differently-separated string that
  merely starts with the right prefix. Static serving of ``/media/*`` is
  jailed against the *configured* media root, not a hardcoded default —
  the reference project's one real regression was static serving quietly
  resolving against the wrong root while the JSON endpoints were correct.
* **/cmd allowlist**: only a fixed set of actions is accepted; anything
  else is a 400 with no detail leaked.
* **No CORS headers are ever sent** — cross-origin browser reads are
  blocked by default same-origin policy.
* **Host allowlist** on every request: the ``Host`` header must name this
  server as ``127.0.0.1``/``localhost``/``[::1]`` + its port — plus, only
  with ``allow_remote``, the bind host and ``airboard.remote_hosts`` — so a
  DNS-rebinding page can neither read notes nor POST.
* **Origin check on POST**: a present ``Origin`` must be ``http://`` + one of
  those same allowed hosts (never the request's own ``Host``). Browsers
  always send it cross-site, so no web page can forge a heartbeat (which
  carries gestures that become JARVIS intents) or a board command; local
  CLI tools send no Origin and keep working.

Endpoints::

    GET  /                    stage.html (fresh on every load; no-store)
    GET  /gestures.js         the named-gesture engine the page imports
    GET  /media/<rel>         media airlock, jailed to the media root
    POST /state                tracker's ~45Hz heartbeat; response carries
                                up to 8 queued commands (piggybacked channel).
                                Its ``hands``/``gestures`` fields are validated
                                and handed to ``on_perception``
    GET  /state                the render page mirrors the scene from here
    POST /cmd                  board commands (agent -> board): {"a": ...}
    GET  /config                {name, orbs: [{title, kind}]} (no paths)
    GET  /tree?orb=N             a notes-orb folder tree, .md only, jailed
    GET  /note?f=N/<rel>         one note's raw text, jailed, .md only
    GET  /props                  media airlock as a browsable tree
    GET  /orb                    the blob's state {state, mood[, wave]}: live
                                  from the kernel bus, else the agent's files
"""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from digital_twin.airboard.orbs import Orb, media_root as compute_media_root
from digital_twin.airboard.orbs import resolve_root

logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS = (
    "add_img", "add_card", "clear", "reset", "hand", "give", "yank",
    "hover", "scroll_note", "widget", "explode", "assemble", "present",
)
_PATH_ACTIONS = ("add_img", "hand", "give", "present")
_MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".webm",
               ".glb", ".gltf"}
_MAX_BODY_BYTES = 262_144  # 256 KiB
_MAX_QUEUED_CMDS = 8
_MOOD_TTL_S = 45.0
_WAVE_TTL_S = 0.6

_STAGE_HTML = Path(__file__).parent / "static" / "stage.html"
_GESTURES_JS = Path(__file__).parent / "static" / "gestures.js"
_HANDS = ("left", "right")
_GESTURE_ID = re.compile(r"^[a-z0-9_]{1,40}$")

#: ``on_perception(hands, gestures)``: hands present this frame, and their
#: stable gestures as ``{"hand", "gesture", "confidence"}`` dicts.
PerceptionCallback = Callable[[list[str], list[dict]], None]


def parse_perception(state: object) -> tuple[list[str], list[dict]] | None:
    """Validate a heartbeat's ``hands``/``gestures``; ``None`` if absent or
    malformed (a bad frame is dropped whole, never half-applied)."""
    if not isinstance(state, dict) or "hands" not in state:
        return None
    hands, gestures = state.get("hands"), state.get("gestures", [])
    if not (isinstance(hands, list) and isinstance(gestures, list)
            and len(hands) <= 2 and len(gestures) <= 2):
        return None
    if not all(h in _HANDS for h in hands) or len(set(hands)) != len(hands):
        return None
    out = []
    for g in gestures:
        if not isinstance(g, dict):
            return None
        hand, name, conf = g.get("hand"), g.get("gesture"), g.get("confidence")
        if (hand not in hands or not isinstance(name, str)
                or not _GESTURE_ID.match(name)
                or isinstance(conf, bool) or not isinstance(conf, (int, float))
                or not math.isfinite(conf) or not 0.0 <= conf <= 1.0):
            return None
        out.append({"hand": hand, "gesture": name, "confidence": float(conf)})
    return list(hands), out


class AirboardServer:
    """Owns the ThreadingHTTPServer and the board's in-memory scene state."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        name: str,
        orbs: list[Orb],
        media_dir: str,
        state_dir: str,
        state_timeout_s: int,
        allow_remote: bool = False,
        remote_hosts: tuple[str, ...] = (),
        on_perception: PerceptionCallback | None = None,
        orb_source: Callable[[], dict] | None = None,
    ):
        self._name = name
        # Names a browser may use to reach this server. Loopback always;
        # with allow_remote, also the bind host and the operator's explicit
        # remote_hosts. Never derived from a request (DNS rebinding).
        self._host_names = ("127.0.0.1", "localhost", "[::1]") + (
            (host, *remote_hosts) if allow_remote else ())
        self._on_perception = on_perception
        self._orb_source = orb_source
        self._orbs = orbs
        self._media_root = compute_media_root(orbs, media_dir)
        self._media_root.mkdir(parents=True, exist_ok=True)
        self._state_dir = resolve_root(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_timeout_s = state_timeout_s

        self._state_lock = threading.Lock()
        self._last_state: bytes = b"{}"
        self._cmds: list[dict] = []

        try:
            page = _STAGE_HTML.read_bytes()
            gestures_js = _GESTURES_JS.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"airboard: missing static file: {exc}") from exc

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                logger.debug("airboard: " + fmt, *args)

            # -- small response helpers ------------------------------------
            def _send(self, code: int, body: bytes, content_type: str,
                      no_store: bool = True) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if no_store:
                    self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode("utf-8"),
                           "application/json")

            def _refused(self, post: bool) -> bool:
                """Host allowlist + POST Origin check; sends the 403 itself.
                Both compare against a fixed set, never against the request's
                own Host header."""
                allowed = outer._allowed_hosts()
                ok = self.headers.get("Host", "") in allowed
                origin = self.headers.get("Origin")
                if post and origin is not None and \
                        origin not in {f"http://{h}" for h in allowed}:
                    ok = False
                if not ok:
                    self._json(403, {"error": "forbidden"})
                return not ok

            # -- GET ---------------------------------------------------------
            def do_GET(self):
                if self._refused(post=False):
                    return
                parsed = urllib.parse.urlsplit(self.path)
                route = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                if route == "/":
                    self._send(200, page, "text/html; charset=utf-8")
                elif route == "/gestures.js":
                    self._send(200, gestures_js, "text/javascript; charset=utf-8")
                elif route.startswith("/media/"):
                    outer._serve_media(self, route[len("/media/"):])
                elif route == "/state":
                    with outer._state_lock:
                        body = outer._last_state
                    self._send(200, body, "application/json")
                elif route == "/config":
                    self._json(200, outer._config_view())
                elif route == "/tree":
                    self._json(200, outer._tree_view(query.get("orb", [""])[0]))
                elif route == "/note":
                    outer._note_view(self, query.get("f", [""])[0])
                elif route == "/props":
                    self._json(200, outer._props_view())
                elif route == "/orb":
                    self._json(200, outer._orb_view())
                else:
                    self._json(404, {"error": "not found"})

            # -- POST ----------------------------------------------------
            def do_POST(self):
                if self._refused(post=True):
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                body = self.rfile.read(length) if 0 < length < _MAX_BODY_BYTES \
                    else b"{}"
                if self.path == "/state":
                    with outer._state_lock:
                        outer._last_state = body
                        drained = outer._cmds[:_MAX_QUEUED_CMDS]
                        del outer._cmds[:_MAX_QUEUED_CMDS]
                    self._json(200, drained)
                    outer._deliver_perception(body)
                elif self.path == "/cmd":
                    ok = outer._accept_cmd(body)
                    self.send_response(204 if ok else 400)
                    self.end_headers()
                else:
                    self._json(404, {"error": "not found"})

        self._closing = False
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Endpoint bodies (kept off the Handler so they're testable directly)
    # ------------------------------------------------------------------
    def _allowed_hosts(self) -> set[str]:
        port = self.port
        return {f"{name}:{port}" for name in self._host_names}

    def _deliver_perception(self, body: bytes) -> None:
        if self._on_perception is None:
            return
        try:
            parsed = parse_perception(json.loads(body))
        except ValueError:
            parsed = None
        if parsed is None:
            return
        try:
            self._on_perception(*parsed)
        except Exception:  # a consumer bug must not break the heartbeat
            logger.exception("airboard: perception callback failed")

    def _serve_media(self, handler: BaseHTTPRequestHandler, rel: str) -> None:
        rel = urllib.parse.unquote(rel).lstrip("/")
        target = (self._media_root / rel).resolve()
        if not (self._media_root == target or self._media_root in target.parents) \
                or not target.is_file():
            handler._json(404, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        handler._send(200, target.read_bytes(), content_type, no_store=False)

    def _accept_cmd(self, body: bytes) -> bool:
        try:
            cmd = json.loads(body)
            if not isinstance(cmd, dict) or cmd.get("a") not in _ALLOWED_ACTIONS:
                return False
            if cmd["a"] in _PATH_ACTIONS and cmd.get("src"):
                cmd["src"] = self._jail_media_src(str(cmd["src"]))
            with self._state_lock:
                self._cmds.append(cmd)
            return True
        except Exception:
            return False

    def _jail_media_src(self, src: str) -> str:
        """Resolve ``src`` inside the media root, self-healing to a unique
        basename match if the exact path misses. Raises on ambiguity/miss
        so the caller (_accept_cmd) turns it into a 400."""
        rel = src.lstrip("/")
        if rel.startswith("media/"):
            rel = rel[len("media/"):]
        target = (self._media_root / rel).resolve()
        if self._media_root in target.parents and target.is_file():
            return "/media/" + target.relative_to(self._media_root).as_posix()
        name = Path(rel).name.lower()
        hits = [p for p in self._media_root.rglob("*")
                if p.is_file() and p.name.lower() == name] if name else []
        if len(hits) != 1:
            raise ValueError("not in the media airlock")
        return "/media/" + hits[0].relative_to(self._media_root).as_posix()

    def _config_view(self) -> dict:
        return {
            "name": self._name,
            "orbs": [{"title": orb.title, "kind": orb.kind}
                     for orb in self._orbs],
        }

    def _notes_root(self, orb_index: str) -> Path | None:
        try:
            orb = self._orbs[int(orb_index)]
        except (ValueError, IndexError):
            return None
        if orb.kind != "notes":
            return None
        return resolve_root(orb.path)

    def _tree_view(self, orb_index: str) -> dict:
        empty = {"name": "?", "notes": [], "dirs": []}
        root = self._notes_root(orb_index)
        if root is None or not root.is_dir():
            return empty
        try:
            return self._walk_notes(root, root, orb_index)
        except OSError:
            return empty

    def _walk_notes(self, directory: Path, root: Path, orb_index: str) -> dict:
        notes = []
        dirs = []
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                sub = self._walk_notes(entry, root, orb_index)
                if sub["notes"] or sub["dirs"]:
                    dirs.append(sub)
            elif entry.suffix.lower() == ".md" and entry.name != "CLAUDE.md":
                notes.append({
                    "title": entry.stem,
                    "file": f"{int(orb_index)}/{entry.relative_to(root).as_posix()}",
                })
        return {"name": directory.name, "notes": notes, "dirs": dirs}

    def _note_view(self, handler: BaseHTTPRequestHandler, f: str) -> None:
        orb_index, _, rel = f.partition("/")
        root = self._notes_root(orb_index)
        if root is None:
            handler._json(404, {"error": "not found"})
            return
        target = (root / rel).resolve()
        if not (root in target.parents and target.suffix.lower() == ".md"
                and target.is_file()):
            handler._json(404, {"error": "not found"})
            return
        text = target.read_text(encoding="utf-8", errors="replace")
        handler._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _props_view(self) -> dict:
        return self._walk_media(self._media_root)

    def _walk_media(self, directory: Path) -> dict:
        items = []
        dirs = []
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                sub = self._walk_media(entry)
                has_readme = (entry / "README.md").is_file()
                if sub["items"] or sub["dirs"] or has_readme:
                    dirs.append({"name": entry.name, **sub})
            elif entry.suffix.lower() in _MEDIA_EXTS:
                items.append(entry.relative_to(self._media_root).as_posix())
        return {"items": items, "dirs": dirs}

    def _orb_view(self) -> dict:
        """The blob's state. The kernel's live bus-derived state wins when
        active; the agent-written state files remain the fallback."""
        out = self._file_orb_view()
        live = self._orb_source() if self._orb_source is not None else {}
        if live.get("state", "idle") != "idle":
            out["state"] = live["state"]
            if live["state"] != "speaking":
                out.pop("wave", None)
        if live.get("mood", "green") != "green":
            out["mood"] = live["mood"]
        return out

    def _file_orb_view(self) -> dict:
        out: dict = {"state": "idle", "mood": "green"}
        now = time.time()
        try:
            state_path = self._state_dir / "state"
            raw = state_path.read_text(encoding="utf-8").strip().lower()
            if raw in ("idle", "listening", "thinking", "speaking"):
                age = now - state_path.stat().st_mtime
                if raw == "idle" or age < self._state_timeout_s:
                    out["state"] = raw
        except Exception:
            pass
        try:
            mood = json.loads((self._state_dir / "mood.json")
                               .read_text(encoding="utf-8"))
            if now - float(mood["ts"]) < _MOOD_TTL_S and \
                    mood.get("mood") in ("green", "amber", "red"):
                out["mood"] = mood["mood"]
        except Exception:
            pass
        if out["state"] == "speaking":
            try:
                wave = json.loads((self._state_dir / "wave.json")
                                   .read_text(encoding="utf-8"))
                if now - float(wave["ts"]) < _WAVE_TTL_S:
                    out["wave"] = {"samples": list(wave["samples"])[:64]}
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="airboard-http", daemon=True,
        )
        self._thread.start()
        logger.info("Air board listening on http://%s:%s",
                    self._server.server_address[0], self.port)

    def stop(self) -> None:
        self._closing = True
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
