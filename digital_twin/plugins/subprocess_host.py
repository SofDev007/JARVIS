"""Subprocess plugin host — the child side of the sandbox bridge.

Run as ``python -m digital_twin.plugins.subprocess_host <plugin_dir>`` by
the loader when a manifest declares ``isolation: subprocess``. The child
loads the plugin exactly as the in-process loader would (same manifest
contract: undeclared actions refused, risk floors applied), then speaks
a line-oriented JSON protocol on stdio:

* child → parent, once: ``{"hello": name, "version": v,
  "actions": [{"name", "risk", "description"}, ...]}``
  (or ``{"fatal": reason}`` and exit)
* parent → child, per call: ``{"id", "action", "params"}``
* child → parent, per call: ``{"id", "ok": true, "detail"}`` or
  ``{"id", "ok": false, "error"}``

What isolation buys, stated precisely: the plugin cannot touch kernel
memory, the secret store, the event bus, or other plugins — its whole
world is (params in, detail out), and a crash or hang costs one action,
never the kernel. What it does not buy: the child is a normal process
running as the user; it is fault + capability isolation, not an OS
sandbox (seccomp/containers remain future work and are documented as
such).

The child deliberately constructs a **collector API**: ``register_module``
and ``api.secret()`` raise — perception modules need the bus and secrets
never leave the parent, so a plugin needing either must earn in-process
trust explicitly.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class _CollectorAPI:
    """The restricted PluginAPI a sandboxed plugin sees."""

    def __init__(self, manifest):
        import logging

        self._manifest = manifest
        self.staged: dict[str, object] = {}
        self.log = logging.getLogger(f"plugin.{manifest.name}")

    @property
    def config(self):
        return dict(self._manifest.config)

    def register_action(self, spec) -> str:
        from digital_twin.plugins.loader import PluginError, _RISK_ORDER

        declared = self._manifest.actions.get(spec.name)
        if declared is None:
            raise PluginError(
                f"action '{spec.name}' is not declared in plugin.yaml — "
                f"declared: {sorted(self._manifest.actions) or '(none)'}"
            )
        effective = (spec.risk if _RISK_ORDER[spec.risk] >= _RISK_ORDER[declared]
                     else declared)
        if spec.name in self.staged:
            raise PluginError(f"action '{spec.name}' registered twice")
        self.staged[spec.name] = (spec, effective)
        return f"{self._manifest.name}.{spec.name}"

    def register_module(self, module) -> None:
        from digital_twin.plugins.loader import PluginError

        raise PluginError(
            "subprocess plugins cannot register perception modules (they "
            "have no event bus); use isolation: in_process for module "
            "plugins"
        )

    def secret(self, name: str) -> str:
        from digital_twin.plugins.loader import PluginError

        raise PluginError(
            "subprocess plugins cannot read secrets — the secret store "
            "never leaves the kernel process; connectors needing "
            "credentials must run in_process (explicit trust)"
        )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        _emit({"fatal": "usage: subprocess_host <plugin_dir>"})
        return 2
    try:
        from digital_twin.plugins.loader import _import_entry
        from digital_twin.plugins.manifest import PluginManifest

        manifest = PluginManifest.from_directory(Path(argv[0]))
        module = _import_entry(manifest)
        register = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(
                f"entry file {manifest.entry.name} does not define register(api)")
        api = _CollectorAPI(manifest)
        register(api)
    except BaseException as exc:  # noqa: BLE001 — everything is fatal here
        _emit({"fatal": f"{type(exc).__name__}: {exc}"})
        return 1

    _emit({
        "hello": manifest.name,
        "version": manifest.version,
        "actions": [
            {"name": name, "risk": effective.value,
             "description": spec.description}
            for name, (spec, effective) in api.staged.items()
        ],
    })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _emit({"id": None, "ok": False, "error": "bad request JSON"})
            continue
        request_id = request.get("id")
        if request.get("op") == "shutdown":
            _emit({"id": request_id, "ok": True, "detail": "bye"})
            return 0
        name = request.get("action")
        staged = api.staged.get(name)
        if staged is None:
            _emit({"id": request_id, "ok": False,
                   "error": f"unknown action {name!r}"})
            continue
        spec, _ = staged
        try:
            params = request.get("params") or {}
            spec.validate(params)
            detail = spec.handler(params)
            _emit({"id": request_id, "ok": True,
                   "detail": "" if detail is None else str(detail)[:1000]})
        except BaseException as exc:  # one action fails; the host survives
            _emit({"id": request_id, "ok": False,
                   "error": f"{type(exc).__name__}: {exc}",
                   "trace": traceback.format_exc(limit=3)[-500:]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
