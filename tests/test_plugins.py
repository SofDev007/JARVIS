"""Tests for the plugin system: the manifest contract, capability
scoping (namespacing + risk floors), two-phase commit, fault isolation,
and a plugin action end to end through the dispatcher gates."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import (
    AutomationConfig,
    PluginsConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.core.registry import ModuleRegistry
from digital_twin.plugins.loader import load_plugins
from digital_twin.plugins.manifest import PluginManifest, PluginManifestError
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy, RiskLevel


def _write_plugin(root: Path, name: str, *, actions: str,
                  body: str, extra_manifest: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        f"name: {name}\n"
        'version: "1.0.0"\n'
        f"description: {name} test plugin\n"
        "entry: plugin.py\n"
        f"actions:\n{actions}"
        f"{extra_manifest}"
    )
    (directory / "plugin.py").write_text(body)
    return directory


_GREETER_BODY = """\
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    units = api.config.get("units", "?")
    api.register_action(ActionSpec(
        name="wave",
        description=f"Wave hello ({units}).",
        risk=RiskLevel.SENSITIVE,
        handler=lambda params: f"wave to {params.get('who', 'world')}",
    ))
"""


# ---------------------------------------------------------------------------
# Manifest contract
# ---------------------------------------------------------------------------
def test_manifest_parses_and_validates(tmp_path):
    directory = _write_plugin(
        tmp_path, "greeter", actions="  wave: sensitive\n",
        body=_GREETER_BODY, extra_manifest="config:\n  units: metric\n",
    )
    manifest = PluginManifest.from_directory(directory)
    assert manifest.name == "greeter"
    assert manifest.actions == {"wave": RiskLevel.SENSITIVE}
    assert manifest.config == {"units": "metric"}
    assert manifest.entry == (directory / "plugin.py").resolve()


@pytest.mark.parametrize("mutation, match", [
    ("name: Bad-Name\n", "name"),
    ("name: greeter\nversion: 1\n", "version"),
    ("name: greeter\nversion: '1'\nentry: ../escape.py\n", "entry"),
    ("name: greeter\nversion: '1'\nentry: plugin.py\nactions: [wave]\n",
     "actions"),
    ("name: greeter\nversion: '1'\nentry: plugin.py\n"
     "actions:\n  wave: catastrophic\n", "risk"),
])
def test_manifest_rejects_contract_violations(tmp_path, mutation, match):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "plugin.py").write_text("def register(api): pass\n")
    (directory / "plugin.yaml").write_text(mutation)
    with pytest.raises(PluginManifestError, match=match):
        PluginManifest.from_directory(directory)


def test_manifest_missing_entry_file(tmp_path):
    directory = tmp_path / "ghost"
    directory.mkdir()
    (directory / "plugin.yaml").write_text(
        "name: ghost\nversion: '1'\nentry: nowhere.py\nactions: {}\n"
    )
    with pytest.raises(PluginManifestError, match="entry file missing"):
        PluginManifest.from_directory(directory)


# ---------------------------------------------------------------------------
# Loading + scoping
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _load(tmp_path, bus) -> tuple[ActionRegistry, list]:
    actions = ActionRegistry()
    modules = ModuleRegistry(bus)
    reports = load_plugins(PluginsConfig(paths=[str(tmp_path)]),
                           actions, modules)
    return actions, reports


def test_plugin_action_is_namespaced_and_configured(tmp_path, bus):
    _write_plugin(tmp_path, "greeter", actions="  wave: sensitive\n",
                  body=_GREETER_BODY,
                  extra_manifest="config:\n  units: metric\n")
    actions, reports = _load(tmp_path, bus)
    assert reports[0].ok and reports[0].actions == ["greeter.wave"]
    spec = actions.get("greeter.wave")
    assert spec is not None
    assert spec.risk is RiskLevel.SENSITIVE
    assert "metric" in spec.description or "metric" in spec.handler({})
    assert actions.get("wave") is None  # never the bare name


def test_undeclared_action_disables_plugin_completely(tmp_path, bus):
    body = _GREETER_BODY.replace('name="wave"', 'name="sneaky"')
    _write_plugin(tmp_path, "greeter", actions="  wave: sensitive\n",
                  body=body)
    actions, reports = _load(tmp_path, bus)
    assert not reports[0].ok
    assert "not declared" in reports[0].error
    assert actions.names == ()


def test_declared_risk_is_a_floor(tmp_path, bus):
    body = _GREETER_BODY.replace("RiskLevel.SENSITIVE", "RiskLevel.SAFE")
    _write_plugin(tmp_path, "wiper", actions="  wave: dangerous\n", body=body)
    actions, reports = _load(tmp_path, bus)
    assert reports[0].ok
    assert actions.get("wiper.wave").risk is RiskLevel.DANGEROUS  # raised


def test_runtime_risk_may_exceed_declaration(tmp_path, bus):
    body = _GREETER_BODY.replace("RiskLevel.SENSITIVE", "RiskLevel.DANGEROUS")
    _write_plugin(tmp_path, "careful", actions="  wave: safe\n", body=body)
    actions, _ = _load(tmp_path, bus)
    assert actions.get("careful.wave").risk is RiskLevel.DANGEROUS


def test_two_phase_commit_no_partial_registration(tmp_path, bus):
    body = _GREETER_BODY + "\n"
    body = body.replace(
        "    api.register_action(ActionSpec(",
        "    api.register_action(ActionSpec(\n"
        "        name='wave', description='ok', risk=RiskLevel.SENSITIVE,\n"
        "        handler=lambda p: 'ok'))\n"
        "    raise RuntimeError('explodes after first registration')\n"
        "    api.register_action(ActionSpec(", 1)
    _write_plugin(tmp_path, "flaky", actions="  wave: sensitive\n", body=body)
    actions, reports = _load(tmp_path, bus)
    assert not reports[0].ok and "explodes" in reports[0].error
    assert actions.names == ()  # the staged action never landed


def test_broken_plugin_is_isolated_from_healthy_ones(tmp_path, bus):
    _write_plugin(tmp_path, "broken", actions="  wave: safe\n",
                  body="import nonexistent_module_xyz\n")
    _write_plugin(tmp_path, "greeter", actions="  wave: sensitive\n",
                  body=_GREETER_BODY)
    actions, reports = _load(tmp_path, bus)
    by_name = {report.name: report for report in reports}
    assert not by_name["broken"].ok
    assert by_name["greeter"].ok
    assert actions.names == ("greeter.wave",)


def test_duplicate_plugin_names_second_is_skipped(tmp_path, bus):
    first = _write_plugin(tmp_path, "greeter", actions="  wave: sensitive\n",
                          body=_GREETER_BODY)
    clone = tmp_path / "zz_clone"
    clone.mkdir()
    (clone / "plugin.yaml").write_text(
        (first / "plugin.yaml").read_text())  # same manifest name
    (clone / "plugin.py").write_text(_GREETER_BODY)
    actions, reports = _load(tmp_path, bus)
    assert [report.ok for report in reports] == [True, False]
    assert "duplicate" in reports[1].error
    assert actions.names == ("greeter.wave",)


def test_module_names_must_carry_the_plugin_prefix(tmp_path, bus):
    body = """\
from digital_twin.core.module import BaseModule

class Rogue(BaseModule):
    name = "memory"   # tries to squat a built-in module name
    topics = ()
    def _on_start(self): pass
    def _on_stop(self): pass

def register(api):
    api.register_module(Rogue())
"""
    _write_plugin(tmp_path, "rogue", actions="", body=body)
    _, reports = _load(tmp_path, bus)
    assert not reports[0].ok
    assert "must start with 'rogue_'" in reports[0].error


def test_empty_paths_load_nothing(bus):
    actions = ActionRegistry()
    reports = load_plugins(PluginsConfig(paths=[]), actions,
                           ModuleRegistry(bus))
    assert reports == [] and actions.names == ()


# ---------------------------------------------------------------------------
# End to end: a plugin action through the full gate pipeline
# ---------------------------------------------------------------------------
def test_plugin_action_runs_through_dispatcher_gates(tmp_path, bus):
    _write_plugin(tmp_path, "greeter", actions="  wave: sensitive\n",
                  body=_GREETER_BODY)
    actions, _ = _load(tmp_path, bus)

    confirmation = ScriptedConfirmation([True])
    security = SecurityConfig()
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=5.0),
        registry=actions,
        policy=PermissionPolicy(security.risk_defaults, security.permissions),
        confirmation=confirmation,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)
    try:
        bus.publish(Event(Topics.ACTION_EXECUTE, "test",
                          {"action": "greeter.wave",
                           "params": {"who": "Sangeetha"}}))
        deadline = time.time() + 3.0
        while not results and time.time() < deadline:
            time.sleep(0.01)
    finally:
        dispatcher.stop()
    assert confirmation.requests  # SENSITIVE default: confirmed
    assert results[0].payload["status"] == "completed"
    assert "wave to Sangeetha" in results[0].payload["detail"]
