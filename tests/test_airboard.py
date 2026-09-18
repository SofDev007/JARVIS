"""Tests for the air board: endpoint contracts and, above all, the path
jail — a /cmd or /media request that tries to escape the configured media
root must be rejected, never served."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from digital_twin.airboard.module import AirboardModule
from digital_twin.airboard.orbs import Orb
from digital_twin.airboard.server import AirboardServer, parse_perception
from digital_twin.configuration.settings import AirboardConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Topics


def _get(port: int, path: str, headers: dict | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(port: int, path: str, body: bytes, headers: dict | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def server(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "sprite.png").write_bytes(b"\x89PNG fake")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "todo.md").write_text("# hi")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")

    orbs = [Orb(title="Notes", path=str(notes_dir), kind="notes"),
            Orb(title="Props", path=str(media_dir), kind="media")]
    srv = AirboardServer(
        "127.0.0.1", 0, name="Test", orbs=orbs,
        media_dir=str(media_dir), state_dir=str(tmp_path / "state"),
        state_timeout_s=600,
    )
    srv.start()
    yield srv, media_dir, notes_dir, outside
    srv.stop()


def test_page_served(server):
    srv, *_ = server
    status, body = _get(srv.port, "/")
    assert status == 200
    assert b"<html" in body.lower()


def test_config_hides_paths(server):
    srv, *_ = server
    status, body = _get(srv.port, "/config")
    assert status == 200
    data = json.loads(body)
    assert data == {"name": "Test", "orbs": [
        {"title": "Notes", "kind": "notes"},
        {"title": "Props", "kind": "media"},
    ]}


def test_cmd_rejects_unknown_action(server):
    srv, *_ = server
    status, _ = _post(srv.port, "/cmd", json.dumps({"a": "delete_everything"}).encode())
    assert status == 400


def test_cmd_accepts_allowed_action(server):
    srv, *_ = server
    status, _ = _post(srv.port, "/cmd", json.dumps({"a": "clear"}).encode())
    assert status == 204


def test_cmd_accepts_media_in_jail_and_rewrites_src(server):
    srv, media_dir, *_ = server
    status, _ = _post(srv.port, "/cmd",
                       json.dumps({"a": "add_img", "src": "sprite.png"}).encode())
    assert status == 204
    # drained via the /state heartbeat's piggybacked command channel
    status, body = _post(srv.port, "/state", b"{}")
    assert status == 200
    cmds = json.loads(body)
    assert cmds == [{"a": "add_img", "src": "/media/sprite.png"}]


def test_cmd_rejects_path_outside_media_root(server):
    """The one regression that matters most: a src that escapes the
    configured media root via traversal must be rejected, not self-healed
    or served."""
    srv, media_dir, notes_dir, outside = server
    status, _ = _post(srv.port, "/cmd", json.dumps(
        {"a": "add_img", "src": "../outside.png"}).encode())
    assert status == 400
    status, _ = _post(srv.port, "/cmd", json.dumps(
        {"a": "add_img", "src": str(outside)}).encode())
    assert status == 400


def test_media_static_serve_is_jailed(server):
    srv, media_dir, *_ = server
    status, body = _get(srv.port, "/media/sprite.png")
    assert status == 200
    assert body == b"\x89PNG fake"
    status, _ = _get(srv.port, "/media/../outside.png")
    assert status == 404


def test_tree_and_note_are_jailed_to_the_notes_orb(server):
    srv, *_ = server
    status, body = _get(srv.port, "/tree?orb=0")
    assert status == 200
    tree = json.loads(body)
    assert tree["notes"] == [{"title": "todo", "file": "0/todo.md"}]

    status, body = _get(srv.port, "/note?f=0/todo.md")
    assert status == 200
    assert body == b"# hi"

    status, _ = _get(srv.port, "/note?f=0/../../outside.png")
    assert status == 404


def test_orb_fails_soft_with_no_state_files(server):
    srv, *_ = server
    status, body = _get(srv.port, "/orb")
    assert status == 200
    assert json.loads(body) == {"state": "idle", "mood": "green"}


# ---------------------------------------------------------------------------
# Browser-origin guards (the heartbeat now carries gestures -> intents)
# ---------------------------------------------------------------------------
def test_gestures_js_served(server):
    srv, *_ = server
    status, body = _get(srv.port, "/gestures.js")
    assert status == 200
    assert b"export class GestureEngine" in body


def test_foreign_host_is_refused(server):
    """DNS rebinding: a page on evil.example resolved to 127.0.0.1."""
    srv, *_ = server
    assert _get(srv.port, "/note?f=0/todo.md",
                {"Host": f"evil.example:{srv.port}"})[0] == 403
    assert _post(srv.port, "/state", b"{}",
                 {"Host": f"evil.example:{srv.port}"})[0] == 403


def test_cross_origin_post_is_refused(server):
    srv, *_ = server
    evil = {"Origin": "http://evil.example"}
    assert _post(srv.port, "/state", b"{}", evil)[0] == 403
    assert _post(srv.port, "/cmd", json.dumps({"a": "clear"}).encode(), evil)[0] == 403


def test_same_origin_and_originless_posts_are_accepted(server):
    srv, *_ = server
    own = {"Origin": f"http://127.0.0.1:{srv.port}"}
    assert _post(srv.port, "/state", b"{}", own)[0] == 200
    assert _post(srv.port, "/cmd", json.dumps({"a": "clear"}).encode())[0] == 204  # CLI


@pytest.mark.parametrize("state, expected", [
    ({}, None),                                               # no perception fields
    ({"hands": []}, ([], [])),
    ({"hands": ["right"], "gestures": [
        {"hand": "right", "gesture": "thumbs_up", "confidence": 0.9}]},
     (["right"], [{"hand": "right", "gesture": "thumbs_up", "confidence": 0.9}])),
    ({"hands": ["middle"]}, None),                            # unknown hand
    ({"hands": ["left", "left"]}, None),                      # duplicate hand
    ({"hands": ["left", "right", "left"]}, None),             # too many
    ({"hands": ["left"], "gestures": [
        {"hand": "right", "gesture": "ok", "confidence": 0.9}]}, None),  # hand not present
    ({"hands": ["left"], "gestures": [
        {"hand": "left", "gesture": "Thumbs Up!", "confidence": 0.9}]}, None),  # bad id
    ({"hands": ["left"], "gestures": [
        {"hand": "left", "gesture": "ok", "confidence": 1.5}]}, None),  # out of range
    ({"hands": ["left"], "gestures": [
        {"hand": "left", "gesture": "ok", "confidence": True}]}, None),  # bool
    ({"hands": ["left"], "gestures": [
        {"hand": "left", "gesture": "ok", "confidence": float("nan")}]}, None),
    ([1, 2], None),
])
def test_parse_perception_validates(state, expected):
    assert parse_perception(state) == expected


# ---------------------------------------------------------------------------
# AirboardModule: heartbeat -> perception.gesture / perception.hand events
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _board(tmp_path, bus, **overrides):
    module = AirboardModule(AirboardConfig(
        port=0, state_dir=str(tmp_path / "state"), media_dir=str(tmp_path / "media"),
        orbs_file=str(tmp_path / "none.yaml"), **overrides))
    module.start(bus)
    return module


@pytest.fixture()
def board(tmp_path, bus):
    module = _board(tmp_path, bus)
    yield module
    module.stop()


def _record(bus, pattern):
    events = []
    bus.subscribe(pattern, events.append)
    return events


def _g(hand, gesture, confidence=0.9):
    return {"hand": hand, "gesture": gesture, "confidence": confidence}


def test_gesture_event_schema_matches_platform_contract(bus, board):
    events = _record(bus, Topics.GESTURE)
    board.on_perception(["right"], [_g("right", "thumbs_up", 0.97)])
    bus.flush(timeout=2.0)
    (event,) = events
    data = event.to_dict()
    assert data["module"] == "airboard"
    assert (data["gesture"], data["hand"], data["repeat"]) == ("thumbs_up", "right", False)
    assert data["confidence"] == pytest.approx(0.97)


def test_held_gesture_publishes_once(bus, board):
    events = _record(bus, Topics.GESTURE)
    for i in range(5):
        board.on_perception(["right"], [_g("right", "thumbs_up")], now=100 + i * 0.02)
    bus.flush(timeout=2.0)
    assert len(events) == 1


def test_gesture_change_and_gap_re_arm(bus, board):
    events = _record(bus, Topics.GESTURE)
    board.on_perception(["right"], [_g("right", "thumbs_up")], now=100.00)
    board.on_perception(["right"], [_g("right", "open_palm")], now=100.02)
    board.on_perception(["right"], [], now=100.04)            # unstable gap
    board.on_perception(["right"], [_g("right", "open_palm")], now=100.06)
    bus.flush(timeout=2.0)
    assert [e.payload["gesture"] for e in events] == ["thumbs_up", "open_palm", "open_palm"]


def test_hand_presence_events(bus, board):
    events = _record(bus, Topics.HAND)
    board.on_perception(["right"], [], now=100.00)
    board.on_perception(["right", "left"], [], now=100.02)
    board.on_perception([], [], now=100.04)
    bus.flush(timeout=2.0)
    assert [(e.payload["hand"], e.payload["present"]) for e in events] == [
        ("right", True), ("left", True), ("left", False), ("right", False)]


def test_two_hands_tracked_independently(bus, board):
    events = _record(bus, Topics.GESTURE)
    board.on_perception(["right", "left"], [_g("right", "thumbs_up"), _g("left", "peace")])
    bus.flush(timeout=2.0)
    assert {(e.payload["hand"], e.payload["gesture"]) for e in events} == {
        ("right", "thumbs_up"), ("left", "peace")}


def test_repeat_interval_republishes_held_gesture(tmp_path, bus):
    module = _board(tmp_path, bus, repeat_interval_s=1.0)
    try:
        events = _record(bus, Topics.GESTURE)
        for now in (100.0, 100.5, 101.0, 101.1):
            module.on_perception(["right"], [_g("right", "thumbs_up")], now=now)
        bus.flush(timeout=2.0)
        assert [e.payload["repeat"] for e in events] == [False, True]
    finally:
        module.stop()


def test_disabled_gestures_and_thresholds(tmp_path, bus):
    module = _board(tmp_path, bus, disabled_gestures=["finger_gun"],
                    gesture_thresholds={"thumbs_up": 0.8})
    try:
        events = _record(bus, Topics.GESTURE)
        module.on_perception(["right"], [_g("right", "finger_gun", 0.99)], now=100.00)
        module.on_perception(["right"], [_g("right", "thumbs_up", 0.7)], now=100.02)
        module.on_perception(["right"], [_g("right", "thumbs_up", 0.85)], now=100.04)
        bus.flush(timeout=2.0)
        assert [e.payload["gesture"] for e in events] == ["thumbs_up"]
    finally:
        module.stop()


def test_heartbeat_gap_re_arms_held_gesture(bus, board):
    """Page closed and reopened while holding the same gesture: fire again."""
    events = _record(bus, Topics.GESTURE)
    board.on_perception(["right"], [_g("right", "ok")], now=100.0)
    board.on_perception(["right"], [_g("right", "ok")], now=105.0)
    bus.flush(timeout=2.0)
    assert len(events) == 2


def test_paused_module_publishes_nothing(bus, board):
    events = _record(bus, "perception.*")
    board.pause()
    board.on_perception(["right"], [_g("right", "thumbs_up")])
    bus.flush(timeout=2.0)
    assert events == []
    board.resume()
    assert board.is_active


def test_http_heartbeat_reaches_the_bus(bus, board):
    events = _record(bus, Topics.GESTURE)
    state = {"hands": ["left"], "gestures": [_g("left", "rock", 0.88)]}
    status, _ = _post(board.port, "/state", json.dumps(state).encode(),
                      {"Origin": f"http://127.0.0.1:{board.port}"})
    assert status == 200
    bus.flush(timeout=2.0)
    assert [(e.payload["hand"], e.payload["gesture"]) for e in events] == [("left", "rock")]
