"""Tests for the security primitives: permissions, confirmation, audit."""

from __future__ import annotations

import json
import threading

import pytest

import logging
import sys

from digital_twin.security.audit import (
    GENESIS,
    AuditLog,
    _ordered_chain_files,
    migrate_audit_chain,
    verify_chain,
    verify_with_anchor,
)
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


# ---------------------------------------------------------------------------
# AuditLog — tamper-evident hash chain (M18 Phase B)
# ---------------------------------------------------------------------------
def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def _rewrite(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_audit_chain_intact_on_append(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for i in range(10):
        audit.record(status="ok", i=i)
    assert audit.verify_chain() is None
    # The first record chains to the genesis constant.
    assert json.loads(_lines(path)[0])["prev"] == GENESIS


def test_audit_chain_detects_middle_mutation(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for i in range(5):
        audit.record(status="ok", i=i)
    lines = _lines(path)
    victim = json.loads(lines[2])
    victim["status"] = "tampered"  # same prev, different content → hash changes
    lines[2] = json.dumps(victim)
    _rewrite(path, lines)
    broken = verify_chain(path)
    assert broken is not None
    # Detected at the successor: record 3 no longer chains to record 2.
    assert broken["index"] == 3


def test_audit_chain_detects_deletion(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for i in range(5):
        audit.record(status="ok", i=i)
    lines = _lines(path)
    del lines[2]
    _rewrite(path, lines)
    broken = verify_chain(path)
    assert broken is not None
    assert broken["index"] == 2  # the record now at slot 2 doesn't chain


def test_audit_chain_detects_reordering(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for i in range(5):
        audit.record(status="ok", i=i)
    lines = _lines(path)
    lines[1], lines[2] = lines[2], lines[1]
    _rewrite(path, lines)
    broken = verify_chain(path)
    assert broken is not None
    assert broken["index"] == 1


def test_audit_chain_survives_process_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path)
    for i in range(3):
        first.record(status="ok", i=i)
    # A fresh instance on the same file simulates a restart: it must resume the
    # chain, not start a new one from genesis.
    second = AuditLog(path)
    second.record(status="ok", i=99)
    assert second.verify_chain() is None
    lines = _lines(path)
    assert len(lines) == 4
    # The appended record chains to the record written by the previous process.
    assert json.loads(lines[3])["prev"] != GENESIS


def test_audit_chain_holds_across_a_rollover_boundary(tmp_path):
    # The seam where chain implementations usually break: the running hash must
    # survive the rollover rename so the first record of the new file chains to
    # the last record of the rolled file.
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, max_bytes=1024)
    for i in range(40):  # forces several rollovers
        audit.record(status="ok", filler="x" * 40, i=i)

    rolled = list(tmp_path.glob("audit-*.jsonl"))
    assert rolled, "expected at least one rollover for this test to mean anything"

    # 1) The whole chain (all rolled files + active) verifies as one.
    assert audit.verify_chain() is None

    # 2) Explicit seam assertion, robust to same-second rolls: the active
    #    file's first record chains to the last rolled file's last record.
    ordered = _ordered_chain_files(path)
    assert ordered[-1] == path and len(ordered) >= 2
    last_rolled = ordered[-2]
    from digital_twin.security.audit import _record_hash

    last_rolled_tail = json.loads(_lines(last_rolled)[-1])
    active_head = json.loads(_lines(path)[0])
    assert active_head["prev"] == _record_hash(last_rolled_tail)
    assert active_head["prev"] != GENESIS  # it did NOT restart the chain

    # 3) A break introduced in a rolled file is still caught across the boundary.
    lines = _lines(last_rolled)
    victim = json.loads(lines[0])
    victim["status"] = "tampered"
    lines[0] = json.dumps(victim)
    _rewrite(last_rolled, lines)
    assert verify_chain(path) is not None


def test_audit_migration_is_non_destructive_and_chains(tmp_path):
    # A legacy, unchained log (records without a 'prev' field).
    legacy = tmp_path / "audit.jsonl"
    records = [json.dumps({"timestamp": f"t{i}", "status": "ok", "i": i})
               for i in range(4)]
    legacy.write_text("\n".join(records) + "\n", encoding="utf-8")
    original = legacy.read_text(encoding="utf-8")

    source, dest = migrate_audit_chain(legacy)
    assert source == legacy
    assert dest == tmp_path / "audit.chained.jsonl"
    assert legacy.read_text(encoding="utf-8") == original  # untouched
    assert verify_chain(dest) is None  # the copy is a valid chain
    assert json.loads(_lines(dest)[0])["prev"] == GENESIS

    with pytest.raises(FileExistsError):  # never overwrites
        migrate_audit_chain(legacy)


def test_audit_startup_warns_on_broken_chain_but_does_not_raise(tmp_path,
                                                                caplog):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for i in range(3):
        audit.record(status="ok", i=i)
    lines = _lines(path)
    victim = json.loads(lines[1])
    victim["status"] = "tampered"
    lines[1] = json.dumps(victim)
    _rewrite(path, lines)

    with caplog.at_level(logging.WARNING):
        AuditLog(path)  # startup verification runs here; must not raise
    assert any("AUDIT CHAIN BROKEN" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# AuditLog — DPAPI tail anchor (M18 Phase 3A): closes the tail-mutation and
# truncation gap the chain alone cannot see. DPAPI is Windows-only.
# ---------------------------------------------------------------------------
_win_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the tail anchor is DPAPI-protected (Windows only)")


def _anchored_log(tmp_path):
    from digital_twin.security.tail_anchor import TailAnchor

    anchor_path = tmp_path / "audit.anchor"
    audit = AuditLog(tmp_path / "audit.jsonl", anchor=TailAnchor(anchor_path))
    return audit, anchor_path


@_win_only
def test_anchor_passes_when_chain_and_tail_are_intact(tmp_path):
    audit, _ = _anchored_log(tmp_path)
    for i in range(4):
        audit.record(status="ok", i=i)
    assert audit.verify_with_anchor() is None


@_win_only
def test_anchor_detects_tail_record_mutation(tmp_path):
    # The last record has no successor, so the chain alone cannot catch this.
    audit, _ = _anchored_log(tmp_path)
    for i in range(4):
        audit.record(status="ok", i=i)
    path = tmp_path / "audit.jsonl"
    lines = _lines(path)
    victim = json.loads(lines[-1])
    victim["status"] = "tampered"  # prev unchanged → chain still "valid"
    lines[-1] = json.dumps(victim)
    _rewrite(path, lines)

    assert verify_chain(path) is None            # chain can't see it…
    broken = audit.verify_with_anchor()          # …but the anchor can.
    assert broken is not None and broken.get("tail_anchor")


@_win_only
def test_anchor_detects_truncation_of_the_last_records(tmp_path):
    audit, _ = _anchored_log(tmp_path)
    for i in range(6):
        audit.record(status="ok", i=i)
    path = tmp_path / "audit.jsonl"
    lines = _lines(path)
    _rewrite(path, lines[:-2])  # drop the last two records

    assert verify_chain(path) is None            # a shorter chain is still valid
    broken = audit.verify_with_anchor()
    assert broken is not None and broken.get("tail_anchor")


@_win_only
def test_missing_anchor_degrades_to_warning_and_still_starts(tmp_path, caplog):
    from digital_twin.security.tail_anchor import TailAnchor

    audit, anchor_path = _anchored_log(tmp_path)
    for i in range(3):
        audit.record(status="ok", i=i)
    anchor_path.unlink()  # simulate an install predating the anchor

    with caplog.at_level(logging.WARNING):
        # Startup verification runs in __init__; a missing anchor must NOT raise.
        restarted = AuditLog(tmp_path / "audit.jsonl",
                             anchor=TailAnchor(anchor_path))
    assert restarted is not None
    assert any("tail anchor" in rec.message.lower() for rec in caplog.records)
    # The module contract: no anchor → anchor_missing, not a hard failure.
    result = verify_with_anchor(tmp_path / "audit.jsonl", None)
    assert result is not None and result.get("anchor_missing")
