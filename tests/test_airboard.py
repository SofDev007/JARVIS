"""Tests for the air board: endpoint contracts and, above all, the path
jail — a /cmd or /media request that tries to escape the configured media
root must be rejected, never served."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from digital_twin.airboard.orbs import Orb
from digital_twin.airboard.server import AirboardServer


def _get(port: int, path: str):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(port: int, path: str, body: bytes):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json"})
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
