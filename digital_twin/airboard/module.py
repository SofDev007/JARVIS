"""The air-board module: a normal :class:`BaseModule` that owns an
:class:`~digital_twin.airboard.server.AirboardServer` for its lifetime.

All hand tracking runs in the browser page (MediaPipe + ``gestures.js``);
Python never opens the camera. The page's heartbeat carries the hands it
sees and their stable named gestures, and this module turns that into the
same semantic bus events the rest of JARVIS consumes:

* ``perception.gesture`` — ``{gesture, confidence, hand, repeat}``,
  edge-triggered on gesture changes, optionally repeated while held.
* ``perception.hand`` — ``{hand, present}`` when hands enter/leave view.

The module never interprets gestures; that belongs to the reasoning layer.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from digital_twin.airboard.orbs import load_orbs
from digital_twin.airboard.server import AirboardServer
from digital_twin.configuration.settings import AirboardConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState

logger = logging.getLogger(__name__)

#: A heartbeat gap this long means the page closed or froze: the next
#: frame starts from a clean slate so a held gesture fires afresh.
_STALE_S = 1.0

# The blob's presence state, derived from the bus. No topic says "done
# listening/thinking/speaking", so each state simply expires.
_LISTEN_S = 8.0
_THINK_S = 30.0
_MOOD_S = 45.0
_MOODS = {"failed": "red", "denied": "amber", "rejected": "amber"}


def _speaking_seconds(text: str) -> float:
    """Rough TTS duration: ~15 characters per second, 1.5–20 s."""
    return max(1.5, min(20.0, len(text) / 15.0))


class AirboardModule(BaseModule):
    """Serve the gesture-controlled overlay board and publish its gestures."""

    name = "airboard"
    topics = (Topics.GESTURE, Topics.HAND)

    def __init__(self, config: AirboardConfig):
        super().__init__()
        self._config = config
        self._server: AirboardServer | None = None
        self._disabled = frozenset(config.disabled_gestures)
        self._thresholds = dict(config.gesture_thresholds)
        # Heartbeats arrive on concurrent HTTP threads; event state is shared.
        self._lock = threading.Lock()
        self._present: set[str] = set()
        self._last_gesture: dict[str, tuple[str, float]] = {}
        self._last_seen = 0.0
        self._orb = ("idle", 0.0)        # (state, expires_at)
        self._mood = ("green", 0.0)
        self._subscriptions: list = []

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("airboard is not running")
        return self._server.port

    def _on_start(self) -> None:
        orbs = load_orbs(self._config.orbs_file)
        self._server = AirboardServer(
            self._config.host,
            self._config.port,
            name=self._config.name,
            orbs=orbs,
            media_dir=self._config.media_dir,
            state_dir=self._config.state_dir,
            state_timeout_s=self._config.state_timeout_s,
            allow_remote=self._config.allow_remote,
            remote_hosts=tuple(self._config.remote_hosts),
            on_perception=self.on_perception,
            orb_source=self.orb_view,
        )
        self._server.start()
        sub = self._bus.subscribe
        self._subscriptions = [
            sub(Topics.VOICE_CONTROL, self._on_voice_control, name="airboard.orb"),
            sub(Topics.VOICE_PARTIAL, lambda e: self._set_orb("listening", _LISTEN_S),
                name="airboard.orb"),
            sub(Topics.VOICE, lambda e: self._set_orb("thinking", _THINK_S),
                name="airboard.orb"),
            sub(Topics.CHAT, lambda e: self._set_orb("thinking", _THINK_S),
                name="airboard.orb"),
            sub(Topics.CHAT_RESPONSE, self._on_reply, name="airboard.orb"),
            sub(Topics.ACTION_RESULT, self._on_action_result, name="airboard.orb"),
        ]

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []
        if self._server is not None:
            self._server.stop()
            self._server = None
        with self._lock:
            self._present = set()
            self._last_gesture = {}

    def _on_pause(self) -> None:
        self._on_stop()

    def _on_resume(self) -> None:
        self._on_start()

    # ------------------------------------------------------------------
    # Bus -> the blob's presence state (served on /orb)
    # ------------------------------------------------------------------
    def _set_orb(self, state: str, seconds: float) -> None:
        self._orb = (state, time.time() + seconds)

    def _on_voice_control(self, event: Event) -> None:
        command = event.payload.get("command")
        if command in ("start", "toggle"):
            self._set_orb("listening", _LISTEN_S)
        elif command == "stop" and self._orb[0] == "listening":
            self._set_orb("idle", 0.0)

    def _on_reply(self, event: Event) -> None:
        self._set_orb("speaking", _speaking_seconds(str(event.payload.get("text") or "")))

    def _on_action_result(self, event: Event) -> None:
        mood = _MOODS.get(event.payload.get("status"))
        if mood:
            self._mood = (mood, time.time() + _MOOD_S)

    def orb_view(self, now: float | None = None) -> dict:
        """``{state, mood}`` with expired states decayed to idle/green."""
        now = time.time() if now is None else now
        state, until = self._orb
        mood, mood_until = self._mood
        return {"state": state if now < until else "idle",
                "mood": mood if now < mood_until else "green"}

    # ------------------------------------------------------------------
    # Heartbeat -> bus events
    # ------------------------------------------------------------------
    def on_perception(
        self, hands: list[str], gestures: list[dict], now: float | None = None
    ) -> None:
        """Diff one validated heartbeat against event state and publish."""
        if self.state is not ModuleState.RUNNING:
            return
        now = time.time() if now is None else now
        current: dict[str, tuple[str, float]] = {}
        for g in gestures:
            # Calibration: disabled gestures and per-gesture confidence
            # floors count as "no stable gesture", which re-arms the edge.
            if g["gesture"] in self._disabled:
                continue
            if g["confidence"] < self._thresholds.get(g["gesture"], 0.0):
                continue
            current[g["hand"]] = (g["gesture"], g["confidence"])
        with self._lock:
            if now - self._last_seen > _STALE_S:
                self._last_gesture = {}
            self._last_seen = now
            self._diff_presence(set(hands))
            self._diff_gestures(current, set(hands), now)

    def _diff_presence(self, hands_present: set[str]) -> None:
        for hand in sorted(hands_present - self._present):
            self._publish_hand(hand, present=True)
        for hand in sorted(self._present - hands_present):
            self._publish_hand(hand, present=False)
            self._last_gesture.pop(hand, None)
        # ponytail: if the page closes mid-gesture no "hand left" event fires
        # until the next heartbeat; add a watchdog if a consumer needs it.
        self._present = hands_present

    def _diff_gestures(
        self,
        current: dict[str, tuple[str, float]],
        hands_present: set[str],
        now: float,
    ) -> None:
        repeat_every = self._config.repeat_interval_s
        for hand, (gesture, confidence) in current.items():
            previous = self._last_gesture.get(hand)
            if previous is None or previous[0] != gesture:
                self._publish_gesture(hand, gesture, confidence, repeat=False)
                self._last_gesture[hand] = (gesture, now)
            elif repeat_every > 0 and now - previous[1] >= repeat_every:
                self._publish_gesture(hand, gesture, confidence, repeat=True)
                self._last_gesture[hand] = (gesture, now)
        # A visible hand with no stable gesture re-arms its edge.
        for hand in list(self._last_gesture):
            if hand in hands_present and hand not in current:
                del self._last_gesture[hand]

    def _publish_gesture(
        self, hand: str, gesture: str, confidence: float, repeat: bool
    ) -> None:
        self._publish(Event(
            topic=Topics.GESTURE,
            source=self.name,
            payload={
                "gesture": gesture,
                "confidence": round(confidence, 3),
                "hand": hand,
                "repeat": repeat,
            },
        ))

    def _publish_hand(self, hand: str, present: bool) -> None:
        self._publish(Event(
            topic=Topics.HAND,
            source=self.name,
            payload={"hand": hand, "present": present},
        ))

    def _metrics(self) -> dict[str, Any]:
        return {"hands_visible": len(self._present)}
