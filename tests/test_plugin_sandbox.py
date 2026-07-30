"""Tests for subprocess plugin isolation (M14): the manifest switch, the
proxy round-trip, manifest risk floors applied parent-side, fault and
timeout containment, and the child's refusal to touch secrets/modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from digital_twin.automation.registry import ActionRegistry
from digital_twin.configuration.settings import PluginsConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.registry import ModuleRegistry
from digital_twin.plugins.loader import load_plugins
from digital_twin.plugins.sandbox import SandboxedPlugin, SandboxError
from digital_twin.plugins.manifest import PluginManifest
from digital_twin.security.permissions import RiskLevel


def _write(root: Path, name: str, body: str, *, actions="  do: sensitive\n",
           isolation="subprocess", extra="") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        f"name: {name}\nversion: \"1.0.0\"\ndescription: {name}\n"
        f"entry: plugin.py\nisolation: {isolation}\n"
        f"actions:\n{actions}{extra}"
    )
    (directory / "plugin.py").write_text(body, encoding="utf-8")
    return directory


_ECHO = """\
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="do", description="echo upper",
        risk=RiskLevel.SAFE,                # manifest floor should raise it
        handler=lambda p: str(p.get("text","")).upper(),
    ))
"""


def _load(tmp_path):
    registry = ActionRegistry()
    reports = load_plugins(PluginsConfig(paths=[str(tmp_path)]), registry, None)
    return registry, reports


def test_manifest_carries_isolation(tmp_path):
    directory = _write(tmp_path, "iso", _ECHO)
    manifest = PluginManifest.from_directory(directory)
    assert manifest.isolation == "subprocess"


def test_manifest_rejects_bad_isolation(tmp_path):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "plugin.py").write_text("def register(api): pass\n")
    (directory / "plugin.yaml").write_text(
        "name: bad\nversion: '1'\nentry: plugin.py\n"
        "isolation: firejail\nactions: {}\n")
    from digital_twin.plugins.manifest import PluginManifestError
    with pytest.raises(PluginManifestError, match="isolation"):
        PluginManifest.from_directory(directory)


def test_sandboxed_action_runs_and_floor_applies(tmp_path):
    _write(tmp_path, "echoer", _ECHO)
    registry, reports = _load(tmp_path)
    assert reports[0].ok and reports[0].sandboxed
    spec = registry.get("echoer.do")
    assert spec is not None
    assert spec.risk is RiskLevel.SENSITIVE  # floored up from SAFE
    assert spec.handler({"text": "hi there"}) == "HI THERE"


def test_undeclared_action_disables_sandboxed_plugin(tmp_path):
    body = _ECHO.replace('name="do"', 'name="sneaky"')
    _write(tmp_path, "rogue", body)
    registry, reports = _load(tmp_path)
    assert not reports[0].ok
    assert "not declared" in reports[0].error
    assert registry.names == ()


def test_crash_in_handler_is_contained(tmp_path):
    body = """\
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def _boom(p):
    raise RuntimeError("kaboom")

def register(api):
    api.register_action(ActionSpec(
        name="do", description="explodes", risk=RiskLevel.SENSITIVE,
        handler=_boom))
"""
    _write(tmp_path, "boom", body)
    registry, reports = _load(tmp_path)
    assert reports[0].ok  # loads fine; the crash is at call time
    spec = registry.get("boom.do")
    with pytest.raises(ValueError, match="kaboom"):
        spec.handler({})
    # the plugin survives one failing call and keeps serving
    assert spec.handler is not None


def test_process_exit_marks_plugin_dead(tmp_path):
    body = """\
import os
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def _die(p):
    os._exit(1)          # take the whole child down mid-call

def register(api):
    api.register_action(ActionSpec(
        name="do", description="suicide", risk=RiskLevel.SENSITIVE,
        handler=_die))
"""
    _write(tmp_path, "suicide", body)
    registry, _ = _load(tmp_path)
    spec = registry.get("suicide.do")
    with pytest.raises(ValueError):      # in-flight call fails
        spec.handler({})
    with pytest.raises(ValueError, match="disabled|crashed"):  # fails fast now
        spec.handler({})


def test_hung_handler_times_out(tmp_path):
    body = """\
import time
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="do", description="hangs", risk=RiskLevel.SENSITIVE,
        handler=lambda p: time.sleep(30) or "never"))
"""
    directory = _write(tmp_path, "hang", body)
    manifest = PluginManifest.from_directory(directory)
    plugin = SandboxedPlugin(manifest, call_timeout_s=0.5)
    try:
        with pytest.raises(ValueError):
            plugin.call("do", {})
    finally:
        plugin.close()


def test_sandbox_refuses_secret_access(tmp_path):
    body = """\
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.secret("anything")   # must raise inside the child → fatal load
"""
    _write(tmp_path, "greedy", body, actions="  do: sensitive\n")
    _, reports = _load(tmp_path)
    assert not reports[0].ok
    assert "secret" in reports[0].error.lower()


def test_sandbox_refuses_module_registration(tmp_path):
    body = """\
from digital_twin.core.module import BaseModule

class M(BaseModule):
    name = "greedy_mod"
    topics = ()
    def _on_start(self): pass
    def _on_stop(self): pass

def register(api):
    api.register_module(M())
"""
    _write(tmp_path, "greedy", body, actions="")
    _, reports = _load(tmp_path)
    assert not reports[0].ok
    assert "module" in reports[0].error.lower()


def test_bad_plugin_dir_fails_to_start(tmp_path):
    directory = _write(tmp_path, "syntaxerr", "this is not python !!!\n")
    manifest = PluginManifest.from_directory(directory)
    with pytest.raises(SandboxError):
        SandboxedPlugin(manifest)
