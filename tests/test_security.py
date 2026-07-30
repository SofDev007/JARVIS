"""Tests for the security primitives: permissions, confirmation, audit."""

from __future__ import annotations

import json
import threading

import pytest

from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import (
    AutoDenyConfirmation,
    ConsoleConfirmation,
    ScriptedConfirmation,
    create_confirmation_provider,
)
from digital_twin.security.permissions import Decision, PermissionPolicy, RiskLevel


# ---------------------------------------------------------------------------
# PermissionPolicy
# ---------------------------------------------------------------------------
def _default_policy(**overrides) -> PermissionPolicy:
    return PermissionPolicy(
        risk_defaults={"safe": "allow", "sensitive": "confirm", "dangerous": "deny"},
        overrides=overrides,
    )


def test_risk_level_defaults():
    policy = _default_policy()
    assert policy.evaluate("log_message", RiskLevel.SAFE) is Decision.ALLOW
    assert policy.evaluate("open_url", RiskLevel.SENSITIVE) is Decision.CONFIRM
    assert policy.evaluate("wipe_disk", RiskLevel.DANGEROUS) is Decision.DENY


def test_per_action_override_wins():
    policy = _default_policy(open_url="allow", log_message="deny")
    assert policy.evaluate("open_url", RiskLevel.SENSITIVE) is Decision.ALLOW
    assert policy.evaluate("log_message", RiskLevel.SAFE) is Decision.DENY


def test_missing_risk_default_denies():
    policy = PermissionPolicy(risk_defaults={}, overrides={})
    assert policy.evaluate("anything", RiskLevel.SAFE) is Decision.DENY


def test_dangerous_allow_is_clamped_to_confirm():
    """The security invariant: dangerous actions can never run unattended."""
    policy = _default_policy(wipe_disk="allow")
    assert policy.evaluate("wipe_disk", RiskLevel.DANGEROUS) is Decision.CONFIRM
    via_default = PermissionPolicy(risk_defaults={"dangerous": "allow"})
    assert via_default.evaluate("wipe_disk", RiskLevel.DANGEROUS) is Decision.CONFIRM


def test_invalid_rule_string_denies():
    policy = PermissionPolicy(risk_defaults={"safe": "yolo"})
    assert policy.evaluate("x", RiskLevel.SAFE) is Decision.DENY


# ---------------------------------------------------------------------------
# Confirmation providers
# ---------------------------------------------------------------------------
def test_auto_deny_always_denies():
    assert AutoDenyConfirmation().request("x", {}, timeout_s=1.0) is False


def test_console_denies_without_tty():
    # pytest's captured stdin is not a TTY → fail-closed path.
    assert ConsoleConfirmation().request("x", {}, timeout_s=1.0) is False


def test_scripted_provider_pops_answers_and_records():
    provider = ScriptedConfirmation([True, False])
    assert provider.request("a", {"k": 1}, 1.0) is True
    assert provider.request("b", {}, 1.0) is False
    assert provider.request("c", {}, 1.0) is False  # exhausted → deny
    assert provider.requests == [("a", {"k": 1}), ("b", {}), ("c", {})]


def test_factory_selects_by_name_and_fails_safe():
    assert isinstance(create_confirmation_provider("console"), ConsoleConfirmation)
    assert isinstance(create_confirmation_provider("auto_deny"), AutoDenyConfirmation)
    assert isinstance(create_confirmation_provider("bogus"), AutoDenyConfirmation)


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------
def test_audit_appends_jsonl_with_timestamp(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record(status="completed", action="notify")
    audit.record(status="denied", action="open_url", detail="policy")

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["status"] == "completed" and "timestamp" in first


def test_audit_tail_returns_latest_entries_oldest_first(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    for i in range(30):
        audit.record(status="completed", i=i)
    tail = audit.tail(5)
    assert [entry["i"] for entry in tail] == [25, 26, 27, 28, 29]
    assert AuditLog(tmp_path / "missing.jsonl").tail() == []


def test_audit_rollover_preserves_history(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=1024)
    for i in range(40):  # each entry ~60 bytes → several rollovers
        audit.record(status="completed", filler="x" * 40, i=i)

    rolled = sorted(tmp_path.glob("audit-*.jsonl"))
    assert rolled, "expected at least one rolled-over audit file"
    total_lines = sum(
        len(p.read_text().splitlines()) for p in [*rolled, tmp_path / "audit.jsonl"]
    )
    assert total_lines == 40  # nothing lost


def test_audit_is_thread_safe(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")

    def writer(n: int) -> None:
        for i in range(50):
            audit.record(status="completed", worker=n, i=i)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 200
    for line in lines:  # every line individually valid JSON (no interleaving)
        json.loads(line)


def test_audit_serialises_non_json_values(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record(status="completed", odd=RiskLevel.SAFE)  # enum → str fallback
    (entry,) = audit.tail(1)
    assert "SAFE" in entry["odd"]
