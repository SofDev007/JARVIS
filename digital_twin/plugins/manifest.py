"""Plugin manifests: the declared-capability contract.

A plugin is a directory containing ``plugin.yaml`` and a Python entry
file. The manifest is not documentation — it is the **contract the
loader enforces**: every action the plugin will register must be declared
here with a risk level, and that declaration is a *floor* (the plugin
can register an action as more dangerous than declared, never less).
Undeclared actions are refused at registration time.

Example ``plugin.yaml``::

    name: weather                # ^[a-z][a-z0-9_]{0,31}$ — the namespace
    version: "1.0.0"
    description: Local weather lookups.
    entry: plugin.py             # relative to the plugin directory
    actions:                     # declared capabilities (name -> min risk)
      current_weather: sensitive
      set_home_city: sensitive
    config:                      # opaque dict handed to the plugin
      units: metric
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from digital_twin.security.permissions import RiskLevel

MANIFEST_FILENAME = "plugin.yaml"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_RISKS = {level.value for level in RiskLevel}


class PluginManifestError(ValueError):
    """The manifest is missing, malformed, or violates the contract."""


@dataclass(frozen=True)
class PluginManifest:
    """Parsed, validated ``plugin.yaml``."""

    name: str
    version: str
    description: str
    entry: Path
    """Absolute path to the plugin's Python entry file."""
    actions: Mapping[str, RiskLevel]
    """Declared action name -> minimum risk (the enforced floor)."""
    isolation: str = "in_process"
    """``in_process`` (full trust, full speed) or ``subprocess`` (the
    plugin runs in a child Python process and its actions are proxied
    over an IPC bridge — a crash or hang cannot take the kernel down,
    and the child has **no access** to kernel memory or the secret
    store)."""
    config: Mapping[str, Any] = field(default_factory=dict)
    directory: Path = Path(".")

    @classmethod
    def from_directory(cls, directory: str | Path) -> "PluginManifest":
        directory = Path(directory).resolve()
        manifest_path = directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise PluginManifestError(f"no {MANIFEST_FILENAME} in {directory}")
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PluginManifestError(f"{manifest_path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise PluginManifestError(f"{manifest_path}: manifest must be a mapping")

        name = raw.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise PluginManifestError(
                f"{manifest_path}: 'name' must match {_NAME_RE.pattern}"
            )
        version = raw.get("version")
        if not isinstance(version, str) or not version.strip():
            raise PluginManifestError(f"{manifest_path}: 'version' must be a string")
        description = str(raw.get("description", "")).strip()

        entry_raw = raw.get("entry")
        if not isinstance(entry_raw, str) or not entry_raw.strip():
            raise PluginManifestError(f"{manifest_path}: 'entry' must be a file name")
        if Path(entry_raw).is_absolute() or ".." in Path(entry_raw).parts:
            raise PluginManifestError(
                f"{manifest_path}: 'entry' must be relative to the plugin "
                "directory (no absolute paths, no '..')"
            )
        entry = (directory / entry_raw).resolve()
        if directory not in entry.parents:
            raise PluginManifestError(
                f"{manifest_path}: 'entry' escapes the plugin directory"
            )
        if not entry.is_file():
            raise PluginManifestError(f"{manifest_path}: entry file missing: {entry}")

        actions_raw = raw.get("actions") or {}
        if not isinstance(actions_raw, dict):
            raise PluginManifestError(
                f"{manifest_path}: 'actions' must map action names to risks"
            )
        actions: dict[str, RiskLevel] = {}
        for action_name, risk in actions_raw.items():
            if not isinstance(action_name, str) or not _NAME_RE.match(action_name):
                raise PluginManifestError(
                    f"{manifest_path}: action name {action_name!r} must match "
                    f"{_NAME_RE.pattern}"
                )
            if not isinstance(risk, str) or risk not in _RISKS:
                raise PluginManifestError(
                    f"{manifest_path}: action {action_name!r} risk must be one "
                    f"of {sorted(_RISKS)}"
                )
            actions[action_name] = RiskLevel(risk)

        config = raw.get("config", {})
        if not isinstance(config, dict):
            raise PluginManifestError(f"{manifest_path}: 'config' must be a mapping")

        isolation = raw.get("isolation", "in_process")
        if isolation not in ("in_process", "subprocess"):
            raise PluginManifestError(
                f"{manifest_path}: 'isolation' must be 'in_process' or "
                f"'subprocess' (got {isolation!r})"
            )

        return cls(
            name=name,
            version=version.strip(),
            description=description,
            entry=entry,
            actions=actions,
            config=config,
            directory=directory,
            isolation=isolation,
        )
