"""Tests for file-system intelligence: containment, refusal semantics,
trash-based delete, and — the point of the milestone — the DANGEROUS
confirm-clamp exercised end to end for the first time."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.file_actions import (
    register_file_actions,
    resolve_roots,
    resolve_within,
)
from digital_twin.automation.registry import ActionRegistry
from digital_twin.configuration.settings import (
    AutomationConfig,
    FilesConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import (
    Decision,
    PermissionPolicy,
    RiskLevel,
)


# ---------------------------------------------------------------------------
# Containment primitive
# ---------------------------------------------------------------------------
def test_resolve_within_accepts_paths_inside_a_root(tmp_path):
    roots = resolve_roots([str(tmp_path)])
    inside = tmp_path / "docs" / "a.txt"
    assert resolve_within(str(inside), roots) == inside.resolve()
    assert resolve_within(str(tmp_path), roots) == tmp_path.resolve()


def test_resolve_within_rejects_escapes_and_garbage(tmp_path):
    roots = resolve_roots([str(tmp_path / "root")])
    (tmp_path / "root").mkdir()
    for bad in (str(tmp_path / "elsewhere.txt"),
                str(tmp_path / "root" / ".." / "escape.txt"),
                "relative/path.txt", "", None, 42):
        with pytest.raises(ValueError):
            resolve_within(bad, roots)


def test_resolve_within_rejects_symlink_escapes(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = root / "link"
    os.symlink(outside, link)
    roots = resolve_roots([str(root)])
    with pytest.raises(ValueError, match="outside the allowed roots"):
        resolve_within(str(link / "secret.txt"), roots)


def test_empty_allow_list_refuses_with_guidance(tmp_path):
    registry = ActionRegistry()
    register_file_actions(registry, FilesConfig(allowed_roots=[]))
    with pytest.raises(ValueError, match="allowed_roots"):
        registry.get("list_files").validate({"path": str(tmp_path)})


# ---------------------------------------------------------------------------
# Read-side actions
# ---------------------------------------------------------------------------
@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes").mkdir()
    (root / "report.txt").write_text("Quarterly revenue up 9%.")
    (root / "notes" / "todo.txt").write_text("ship M10")
    (root / "notes" / "copy_of_report.txt").write_text("Quarterly revenue up 9%.")
    return root


def _registry(root: Path, **overrides) -> ActionRegistry:
    registry = ActionRegistry()
    register_file_actions(
        registry, FilesConfig(allowed_roots=[str(root)], **overrides)
    )
    return registry


def test_expected_actions_and_risk_stratification(workspace):
    registry = _registry(workspace)
    sensitive = {"list_files", "search_files", "read_text_file",
                 "find_duplicates", "create_directory", "write_text_file",
                 "copy_file"}
    dangerous = {"move_file", "delete_file"}
    assert set(registry.names) == sensitive | dangerous
    for name in sensitive:
        assert registry.get(name).risk is RiskLevel.SENSITIVE
    for name in dangerous:
        assert registry.get(name).risk is RiskLevel.DANGEROUS


def test_list_files_flat_recursive_and_trash_hidden(workspace):
    registry = _registry(workspace)
    handler = registry.get("list_files").handler
    flat = handler({"path": str(workspace)})
    assert "report.txt" in flat and "notes/" in flat and "todo" not in flat
    deep = handler({"path": str(workspace), "recursive": True})
    assert "todo.txt" in deep

    trash = workspace / ".digital_twin_trash"
    trash.mkdir()
    (trash / "old.txt").write_text("x")
    assert "old.txt" not in handler({"path": str(workspace), "recursive": True})


def test_search_and_read_and_duplicates(workspace):
    registry = _registry(workspace)
    found = registry.get("search_files").handler(
        {"path": str(workspace), "query": "REPORT"})
    assert "report.txt" in found and "copy_of_report.txt" in found

    read = registry.get("read_text_file").handler(
        {"path": str(workspace / "report.txt")})
    assert "Quarterly revenue up 9%." in read

    duplicates = registry.get("find_duplicates").handler(
        {"path": str(workspace)})
    assert "1 duplicate group" in duplicates
    assert "report.txt" in duplicates and "copy_of_report.txt" in duplicates


def test_read_is_bounded(workspace):
    registry = _registry(workspace, max_read_chars=5)
    read = registry.get("read_text_file").handler(
        {"path": str(workspace / "report.txt")})
    assert "Quart" in read and "revenue" not in read and "truncated" in read


def test_list_is_capped(workspace):
    registry = _registry(workspace, max_list_entries=2)
    for index in range(5):
        (workspace / f"file{index}.txt").write_text("x")
    listing = registry.get("list_files").handler({"path": str(workspace)})
    assert "…capped" in listing


# ---------------------------------------------------------------------------
# Write-side refusal semantics
# ---------------------------------------------------------------------------
def test_write_creates_new_but_never_overwrites(workspace):
    registry = _registry(workspace)
    spec = registry.get("write_text_file")
    target = workspace / "new.txt"
    spec.validate({"path": str(target), "text": "hello"})
    assert "wrote 5 characters" in spec.handler(
        {"path": str(target), "text": "hello"})
    assert target.read_text() == "hello"
    # Validation refuses an existing target…
    with pytest.raises(ValueError, match="refusing to overwrite"):
        spec.validate({"path": str(target), "text": "clobber"})
    # …and the handler refuses too (open('x')), even if validation is stale.
    with pytest.raises(FileExistsError):
        spec.handler({"path": str(target), "text": "clobber"})
    assert target.read_text() == "hello"


def test_write_requires_existing_parent_and_bounds_size(workspace):
    registry = _registry(workspace, max_write_chars=10)
    spec = registry.get("write_text_file")
    with pytest.raises(ValueError, match="exceeds 10"):
        spec.validate({"path": str(workspace / "big.txt"), "text": "x" * 11})
    with pytest.raises(ValueError, match="create_directory"):
        spec.handler({"path": str(workspace / "missing" / "a.txt"),
                      "text": "hi"})


def test_create_directory_copy_and_move(workspace):
    registry = _registry(workspace)
    registry.get("create_directory").handler(
        {"path": str(workspace / "archive")})
    assert (workspace / "archive").is_dir()
    with pytest.raises(ValueError, match="already exists"):
        registry.get("create_directory").validate(
            {"path": str(workspace / "archive")})

    copy = registry.get("copy_file")
    copy.handler({"source": str(workspace / "report.txt"),
                  "destination": str(workspace / "archive" / "report.txt")})
    assert (workspace / "archive" / "report.txt").read_text() \
        == "Quarterly revenue up 9%."
    with pytest.raises(ValueError, match="already exists"):
        copy.validate({"source": str(workspace / "report.txt"),
                       "destination": str(workspace / "archive" / "report.txt")})

    move = registry.get("move_file")
    move.handler({"source": str(workspace / "notes" / "todo.txt"),
                  "destination": str(workspace / "archive" / "todo.txt")})
    assert not (workspace / "notes" / "todo.txt").exists()
    assert (workspace / "archive" / "todo.txt").read_text() == "ship M10"


def test_delete_moves_to_trash_and_is_recoverable(workspace):
    registry = _registry(workspace)
    spec = registry.get("delete_file")
    detail = spec.handler({"path": str(workspace / "report.txt")})
    assert "moved to trash" in detail
    assert not (workspace / "report.txt").exists()
    trash_files = list((workspace / ".digital_twin_trash").iterdir())
    assert len(trash_files) == 1
    assert trash_files[0].name.endswith("_report.txt")
    assert trash_files[0].read_text() == "Quarterly revenue up 9%."
    # Deleting from the trash itself is refused — nothing is ever destroyed.
    with pytest.raises(ValueError, match="trash"):
        spec.validate({"path": str(trash_files[0])})


def test_validators_reject_out_of_root_params_before_any_gate(workspace,
                                                              tmp_path):
    registry = _registry(workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    cases = [
        ("list_files", {"path": str(tmp_path)}),
        ("read_text_file", {"path": str(outside)}),
        ("write_text_file", {"path": str(tmp_path / "new.txt"), "text": "x"}),
        ("delete_file", {"path": str(outside)}),
        ("move_file", {"source": str(workspace / "report.txt"),
                       "destination": str(tmp_path / "stolen.txt")}),
        ("copy_file", {"source": str(outside),
                       "destination": str(workspace / "in.txt")}),
    ]
    for name, params in cases:
        with pytest.raises(ValueError):
            registry.get(name).validate(params)


# ---------------------------------------------------------------------------
# THE milestone test: DANGEROUS + configured allow still confirms (the
# clamp M3 has enforced in code since 0.3.0, exercised by a real action).
# ---------------------------------------------------------------------------
class _Pipeline:
    def __init__(self, bus, tmp_path, root, answers=None, permissions=None,
                 risk_defaults=None):
        self.registry = ActionRegistry()
        register_file_actions(
            self.registry, FilesConfig(allowed_roots=[str(root)])
        )
        self.confirmation = ScriptedConfirmation(answers or [])
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        security = SecurityConfig(
            permissions=permissions or {},
            **({"risk_defaults": risk_defaults} if risk_defaults else {}),
        )
        self.dispatcher = ActionDispatcher(
            config=AutomationConfig(action_timeout_s=5.0),
            registry=self.registry,
            policy=PermissionPolicy(security.risk_defaults,
                                    security.permissions),
            confirmation=self.confirmation,
            audit=self.audit,
        )
        self.results = []
        bus.subscribe(Topics.ACTION_RESULT, self.results.append)

    def execute(self, bus, action, params):
        bus.publish(Event(Topics.ACTION_EXECUTE, "test",
                          {"action": action, "params": params}))

    def wait(self, count=1, timeout=3.0):
        deadline = time.time() + timeout
        while len(self.results) < count and time.time() < deadline:
            time.sleep(0.01)
        return self.results


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def test_policy_clamps_dangerous_allow_to_confirm_for_delete_file():
    policy = PermissionPolicy(
        risk_defaults={"safe": "allow", "sensitive": "confirm",
                       "dangerous": "deny"},
        overrides={"delete_file": "allow"},  # a config typo / trickery
    )
    assert policy.evaluate("delete_file", RiskLevel.DANGEROUS) \
        is Decision.CONFIRM


def test_dangerous_delete_confirms_despite_allow_and_executes_on_approval(
    bus, tmp_path, workspace
):
    pipeline = _Pipeline(bus, tmp_path, workspace, answers=[True],
                         permissions={"delete_file": "allow"})
    pipeline.dispatcher.start(bus)
    try:
        pipeline.execute(bus, "delete_file",
                         {"path": str(workspace / "report.txt")})
        results = pipeline.wait()
    finally:
        pipeline.dispatcher.stop()
    # The clamp: 'allow' was configured, yet the human was still asked.
    assert pipeline.confirmation.requests == [
        ("delete_file", {"path": str(workspace / "report.txt")})
    ]
    assert results[0].payload["status"] == "completed"
    assert not (workspace / "report.txt").exists()
    assert list((workspace / ".digital_twin_trash").iterdir())


def test_dangerous_delete_rejected_when_confirmation_denied(bus, tmp_path,
                                                            workspace):
    pipeline = _Pipeline(bus, tmp_path, workspace, answers=[False],
                         permissions={"delete_file": "allow"})
    pipeline.dispatcher.start(bus)
    try:
        pipeline.execute(bus, "delete_file",
                         {"path": str(workspace / "report.txt")})
        results = pipeline.wait()
    finally:
        pipeline.dispatcher.stop()
    assert results[0].payload["status"] == "rejected"
    assert (workspace / "report.txt").exists()  # nothing happened


def test_dangerous_denied_outright_by_default_risk_rule(bus, tmp_path,
                                                        workspace):
    pipeline = _Pipeline(bus, tmp_path, workspace)  # defaults: dangerous=deny
    pipeline.dispatcher.start(bus)
    try:
        pipeline.execute(bus, "move_file",
                         {"source": str(workspace / "report.txt"),
                          "destination": str(workspace / "renamed.txt")})
        results = pipeline.wait()
    finally:
        pipeline.dispatcher.stop()
    assert results[0].payload["status"] == "denied"
    assert pipeline.confirmation.requests == []
    assert (workspace / "report.txt").exists()


def test_out_of_root_step_is_invalid_before_any_confirmation(bus, tmp_path,
                                                             workspace):
    pipeline = _Pipeline(bus, tmp_path, workspace, answers=[True])
    pipeline.dispatcher.start(bus)
    try:
        pipeline.execute(bus, "delete_file", {"path": "/etc/passwd"})
        results = pipeline.wait()
    finally:
        pipeline.dispatcher.stop()
    assert results[0].payload["status"] == "invalid"
    # The user was never asked to confirm garbage.
    assert pipeline.confirmation.requests == []
