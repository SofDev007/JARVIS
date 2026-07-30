"""Parent side of the subprocess plugin sandbox.

:class:`SandboxedPlugin` owns one child process (spawned lazily is
wrong here — the handshake *is* the load, so the child starts at load
time and a broken plugin is disabled immediately, same as in-process)
and turns each declared action into a proxy :class:`ActionSpec` whose
handler performs one request/response over the pipe.

Failure semantics, all tested:

* child dies / pipe breaks → the in-flight action **fails**, the plugin
  is marked dead, subsequent calls fail fast with guidance — the kernel
  and every other plugin are untouched;
* per-call timeout → the child is killed (a hung plugin must not pin a
  dispatcher worker) and the action fails;
* kernel shutdown / interpreter exit → children are terminated via
  ``atexit``.

Risk levels for proxy actions come from the child's handshake, floored
by the manifest exactly like in-process plugins — a lying child can make
its actions *more* guarded, never less, because the parent re-applies
the manifest floor itself.
"""

from __future__ import annotations

import atexit
import itertools
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from digital_twin.automation.registry import ActionSpec
from digital_twin.plugins.manifest import PluginManifest
from digital_twin.security.permissions import RiskLevel

logger = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT_S = 15.0


class SandboxError(RuntimeError):
    """The child failed to start, answer, or stay alive."""


class SandboxedPlugin:
    """One child process + the proxy actions that talk to it."""

    def __init__(self, manifest: PluginManifest,
                 call_timeout_s: float = 20.0):
        self._manifest = manifest
        self._timeout = call_timeout_s
        self._lock = threading.Lock()  # one in-flight request at a time
        self._ids = itertools.count(1)
        self._dead: str | None = None
        self._process = subprocess.Popen(
            [sys.executable, "-m", "digital_twin.plugins.subprocess_host",
             str(manifest.directory)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(Path(__file__).resolve().parents[2]),  # repo root
        )
        atexit.register(self.close)
        hello = self._read(timeout_s=_HANDSHAKE_TIMEOUT_S)
        if "fatal" in hello:
            self.close()
            raise SandboxError(f"plugin failed in its sandbox: {hello['fatal']}")
        if hello.get("hello") != manifest.name:
            self.close()
            raise SandboxError(
                f"handshake mismatch: expected {manifest.name!r}, "
                f"got {hello.get('hello')!r}"
            )
        self.reported_actions: list[dict[str, Any]] = hello.get("actions", [])
        logger.info("Sandboxed plugin %s %s up (pid %s, %d action(s))",
                    manifest.name, manifest.version, self._process.pid,
                    len(self.reported_actions))

    # ------------------------------------------------------------------
    def _read(self, timeout_s: float) -> dict[str, Any]:
        """Read one JSON line from the child with a hard timeout."""
        result: dict[str, Any] = {}

        def reader() -> None:
            line = self._process.stdout.readline()
            if line:
                try:
                    result.update(json.loads(line))
                except json.JSONDecodeError:
                    result["fatal"] = f"bad JSON from child: {line[:120]!r}"
            else:
                result["fatal"] = "child closed its pipe"

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout_s)
        if thread.is_alive():
            self._process.kill()
            raise SandboxError(
                f"plugin '{self._manifest.name}' did not answer within "
                f"{timeout_s:.0f}s — killed"
            )
        if not result:
            raise SandboxError(f"plugin '{self._manifest.name}' sent nothing")
        return result

    def call(self, action: str, params: Mapping[str, Any]) -> str:
        """One request/response; used by every proxy handler."""
        with self._lock:
            if self._dead:
                raise ValueError(
                    f"plugin '{self._manifest.name}' is disabled: {self._dead}")
            if self._process.poll() is not None:
                self._dead = "its process exited"
                raise ValueError(
                    f"plugin '{self._manifest.name}' crashed earlier — "
                    "restart the kernel to reload it")
            request_id = f"r{next(self._ids)}"
            try:
                self._process.stdin.write(json.dumps(
                    {"id": request_id, "action": action,
                     "params": dict(params)}) + "\n")
                self._process.stdin.flush()
                answer = self._read(timeout_s=self._timeout)
            except (SandboxError, OSError, BrokenPipeError) as exc:
                self._dead = str(exc)
                raise ValueError(
                    f"plugin '{self._manifest.name}' failed: {exc} — the "
                    "kernel is unaffected; restart to reload the plugin"
                ) from exc
            if "fatal" in answer:
                self._dead = answer["fatal"]
                raise ValueError(
                    f"plugin '{self._manifest.name}' died mid-call "
                    f"({answer['fatal']}) — the kernel is unaffected; "
                    "restart to reload the plugin")
            if answer.get("id") != request_id:
                self._dead = "protocol desync"
                self._process.kill()
                raise ValueError(
                    f"plugin '{self._manifest.name}' answered out of order "
                    "— killed (kernel unaffected)")
            if not answer.get("ok"):
                raise ValueError(str(answer.get("error", "plugin error")))
            return str(answer.get("detail", ""))

    # ------------------------------------------------------------------
    def proxy_specs(self) -> list[ActionSpec]:
        """Namespaced, manifest-floored proxy specs for the registry."""
        from digital_twin.plugins.loader import _RISK_ORDER

        specs: list[ActionSpec] = []
        for entry in self.reported_actions:
            name = str(entry.get("name", ""))
            declared = self._manifest.actions.get(name)
            if declared is None:
                # The child already refuses these; double-refusal on the
                # parent side keeps a compromised child contained.
                logger.error("Sandboxed plugin %s reported undeclared "
                             "action %r — ignored", self._manifest.name, name)
                continue
            try:
                reported = RiskLevel(str(entry.get("risk", "dangerous")))
            except ValueError:
                reported = RiskLevel.DANGEROUS
            effective = (reported
                         if _RISK_ORDER[reported] >= _RISK_ORDER[declared]
                         else declared)
            specs.append(ActionSpec(
                name=f"{self._manifest.name}.{name}",
                description=(f"[{self._manifest.name} plugin, sandboxed] "
                             f"{entry.get('description', '')}"),
                risk=effective,
                handler=(lambda params, _n=name: self.call(_n, params)),
            ))
        return specs

    @property
    def alive(self) -> bool:
        return self._dead is None and self._process.poll() is None

    def close(self) -> None:
        if self._process.poll() is None:
            try:
                self._process.stdin.write(
                    json.dumps({"id": "bye", "op": "shutdown"}) + "\n")
                self._process.stdin.flush()
            except OSError:
                pass
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._dead = self._dead or "closed"
