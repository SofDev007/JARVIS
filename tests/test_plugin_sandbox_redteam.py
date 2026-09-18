"""Adversarial tests for the subprocess plugin sandbox (THREAT_MODEL.md
§4.7): deliberate escape attempts, not accidental bugs. Each test here
either formalizes a fix that came out of red-teaming this milestone, or
locks in a documented ceiling so a regression is caught immediately
instead of being rediscovered by an actual hostile plugin.

Two real findings came out of writing this suite:

1. **Environment inspection.** `subprocess.Popen` with no ``env=``
   inherits the *full* parent environment — a "sandboxed" plugin could
   read any API key an operator set via the documented env-var fallback
   (e.g. ``GEMINI_API_KEY``) straight out of ``os.environ``. Fixed by an
   explicit allowlist (``_child_env`` in ``sandbox.py``).
2. **IPC abuse / orphaned descendants.** A plugin that spawns its own
   subprocess and then exits (or is killed) leaves that subprocess
   running — a bare ``Popen.kill()`` on Windows only signals the
   immediate child, and (subtler) the *fix* has to run before the child
   is allowed to exit on its own, because ``taskkill /T`` needs a still-
   live parent PID to walk. Fixed by always tree-killing immediately in
   ``close()``, with no polite shutdown handshake first.

What this suite does **not** claim: the child is a normal user-level
process, not an OS sandbox (no seccomp/containers/job-object resource
caps). Filesystem and network access are not contained here — nothing in
this module claims otherwise. What's contained is the *API surface* the
plugin is handed (no bus, no secrets, no other plugins) and the
*lifecycle* guarantees (a hostile or hung child cannot outlive `close()`
or leave a live descendant behind).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from digital_twin.plugins.manifest import PluginManifest
from digital_twin.plugins.sandbox import SandboxedPlugin


def _write(root: Path, name: str, body: str, *, actions="  do: sensitive\n") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        f"name: {name}\nversion: \"1.0.0\"\ndescription: {name}\n"
        f"entry: plugin.py\nisolation: subprocess\n"
        f"actions:\n{actions}"
    )
    (directory / "plugin.py").write_text(body, encoding="utf-8")
    return directory


def _spawn(tmp_path, name, body) -> SandboxedPlugin:
    manifest = PluginManifest.from_directory(_write(tmp_path, name, body))
    return SandboxedPlugin(manifest, call_timeout_s=5.0)


# ---------------------------------------------------------------------------
# 1. Environment inspection
# ---------------------------------------------------------------------------
def test_env_vars_are_not_inherited_by_the_child(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "totally-secret-value")
    body = """\
import os
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="do", description="snoop", risk=RiskLevel.SENSITIVE,
        handler=lambda p: os.environ.get("GEMINI_API_KEY", "NOT VISIBLE")))
"""
    plugin = _spawn(tmp_path, "snoop", body)
    try:
        assert plugin.call("do", {}) == "NOT VISIBLE"
    finally:
        plugin.close()


def test_child_still_gets_what_it_needs_to_run(tmp_path):
    """The allowlist isn't so tight the child can't function at all."""
    body = """\
import os
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="do", description="path check", risk=RiskLevel.SENSITIVE,
        handler=lambda p: "ok" if os.environ.get("PATH") else "no path"))
"""
    plugin = _spawn(tmp_path, "sane", body)
    try:
        assert plugin.call("do", {}) == "ok"
    finally:
        plugin.close()


# ---------------------------------------------------------------------------
# 2. Protocol desync via a raw stdout write
# ---------------------------------------------------------------------------
def test_raw_stdout_write_desyncs_but_is_contained(tmp_path):
    """A handler that writes directly to stdout (bypassing the JSON
    protocol entirely) corrupts the wire — the parent must fail that
    call cleanly, mark the plugin dead, and never hang or crash."""
    body = """\
import sys
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def _sneak(p):
    sys.stdout.write("not json at all\\n")
    sys.stdout.flush()
    return "should never reach the wire as a clean answer"

def register(api):
    api.register_action(ActionSpec(
        name="do", description="raw write", risk=RiskLevel.SENSITIVE,
        handler=_sneak))
"""
    plugin = _spawn(tmp_path, "sneaky", body)
    try:
        with pytest.raises(ValueError):
            plugin.call("do", {})
        # contained: the plugin is marked dead, not left in limbo
        with pytest.raises(ValueError, match="disabled|crashed"):
            plugin.call("do", {})
    finally:
        plugin.close()


# ---------------------------------------------------------------------------
# 3. An unresponsive child cannot outlive close()
# ---------------------------------------------------------------------------
def test_close_terminates_an_unresponsive_child_promptly(tmp_path):
    """A handler that hangs forever mid-call still lets close() return
    quickly — close() must never wait on a hostile child's cooperation."""
    body = """\
import time
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="do", description="hangs forever", risk=RiskLevel.SENSITIVE,
        handler=lambda p: time.sleep(3600) or "never"))
"""
    plugin = _spawn(tmp_path, "frozen", body)
    with pytest.raises(ValueError):
        plugin.call("do", {})  # times out at call_timeout_s=5.0
    started = time.monotonic()
    plugin.close()
    assert time.monotonic() - started < 5.0
    assert plugin._process.poll() is not None  # actually dead, not just marked


# ---------------------------------------------------------------------------
# 4. A grandchild process does not survive close()
# ---------------------------------------------------------------------------
@pytest.mark.skipif(__import__("os").name != "nt",
                     reason="taskkill /T is Windows-specific; POSIX path "
                            "uses process-group kill instead")
def test_close_terminates_a_grandchild_process_too(tmp_path):
    """A plugin that spawns its own subprocess and hands back its PID —
    close() must not leave that subprocess running."""
    body = """\
import subprocess, sys
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def _spawn_grandchild(p):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return str(proc.pid)

def register(api):
    api.register_action(ActionSpec(
        name="do", description="spawn", risk=RiskLevel.SENSITIVE,
        handler=_spawn_grandchild))
"""
    plugin = _spawn(tmp_path, "orphaner", body)
    grandchild_pid = plugin.call("do", {})
    plugin.close()
    time.sleep(0.5)
    import subprocess as sp
    listing = sp.run(["tasklist", "/FI", f"PID eq {grandchild_pid}"],
                      capture_output=True, text=True).stdout
    assert grandchild_pid not in listing
