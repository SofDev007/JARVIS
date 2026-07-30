"""Plugin loader: discovery, capability scoping and fault isolation.

What loading a plugin means here, stated honestly:

* **Trust is explicit.** Only directories named in ``plugins.paths`` are
  scanned. An empty list — the default — loads nothing, ever.
* **The manifest is enforced, not advisory.** A plugin's ``register()``
  receives a :class:`PluginAPI` — the *only* surface it gets — and every
  capability flows through it: actions must be declared in the manifest
  (undeclared → refused), the declared risk is a floor (a plugin may
  register an action as riskier than declared, never safer), and every
  action and module is namespaced ``<plugin>.<name>`` / ``<plugin>_…``
  so built-ins can never be shadowed or replaced.
* **Failures are isolated.** A broken manifest, an import error, or an
  exception inside ``register()`` disables that one plugin (recorded on
  its :class:`LoadedPlugin` report) — the kernel and other plugins are
  unaffected, the M1 fault-isolation principle applied to load time.
* **Runtime is unchanged.** Plugin actions execute through the same
  dispatcher pipeline as built-ins: permission policy (with the
  DANGEROUS confirm-clamp), confirmation gates, audit with provenance.

And the limit, stated just as honestly: **in-process Python cannot be
sandboxed.** A malicious plugin imported into the interpreter can do
anything the process can. This module gives least-privilege *scoping*
for honest plugins and a contract that makes capabilities reviewable;
OS-level isolation (subprocess plugins with an IPC bridge) is the real
sandbox and is tracked in REMAINING_WORK §2.11.

A plugin entry file implements one function::

    def register(api):                       # api: PluginAPI
        api.register_action(ActionSpec(
            name="current_weather",          # becomes weather.current_weather
            description="...",
            risk=RiskLevel.SENSITIVE,
            handler=...,
        ))
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import PluginsConfig
from digital_twin.core.module import BaseModule
from digital_twin.core.registry import ModuleRegistry
from digital_twin.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifest,
    PluginManifestError,
)
from digital_twin.security.permissions import RiskLevel

logger = logging.getLogger(__name__)

_RISK_ORDER = {
    RiskLevel.SAFE: 0,
    RiskLevel.SENSITIVE: 1,
    RiskLevel.DANGEROUS: 2,
}


class PluginError(RuntimeError):
    """A plugin violated its contract at registration time."""


@dataclass
class LoadedPlugin:
    """Per-plugin load report — the startup line of truth."""

    name: str
    version: str = "?"
    directory: Path | None = None
    actions: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    error: str | None = None
    sandboxed: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


class PluginAPI:
    """The entire surface a plugin gets — capability scoping lives here.

    Registration is **two-phase**: calls during ``register()`` validate
    the contract and *stage* capabilities; the loader commits them only
    after ``register()`` returns cleanly. A plugin that fails halfway
    therefore contributes nothing at all — no partial capability sets.
    """

    def __init__(
        self,
        manifest: PluginManifest,
        action_registry: ActionRegistry | None,
        module_registry: ModuleRegistry | None,
        report: LoadedPlugin,
        secrets=None,  # SecretStore | None
    ):
        self._manifest = manifest
        self._actions = action_registry
        self._modules = module_registry
        self._report = report
        self._pending_actions: list[ActionSpec] = []
        self._pending_modules: list[BaseModule] = []
        self._secrets = secrets
        self.log = logging.getLogger(f"digital_twin.plugins.{manifest.name}")

    @property
    def config(self) -> Mapping[str, Any]:
        """The plugin's opaque ``config`` mapping from its manifest."""
        return dict(self._manifest.config)

    def secret(self, name: str) -> str:
        """Resolve a secret **by name** from the kernel's secret store.

        Part of the declared trust surface: a loaded plugin may read any
        named secret (in-process code could anyway — this makes the
        capability explicit instead of pretending otherwise). Values must
        follow the M11 rule — used at the last moment, never logged,
        never placed in params/results/audit.
        """
        if self._secrets is None:
            raise PluginError(
                f"plugin '{self._manifest.name}': no secret store available"
            )
        return self._secrets.get(name)

    # ------------------------------------------------------------------
    def register_action(self, spec: ActionSpec) -> str:
        """Stage a declared action, namespaced and risk-floored.

        Returns the namespaced action name that will be registered.
        """
        if self._actions is None:
            raise PluginError(
                f"plugin '{self._manifest.name}': automation is disabled — "
                "no actions can be registered"
            )
        declared = self._manifest.actions.get(spec.name)
        if declared is None:
            raise PluginError(
                f"plugin '{self._manifest.name}': action '{spec.name}' is "
                f"not declared in {MANIFEST_FILENAME} — declared actions: "
                f"{sorted(self._manifest.actions) or '(none)'}"
            )
        # The declared risk is a floor: never registered below it.
        effective = (
            spec.risk
            if _RISK_ORDER[spec.risk] >= _RISK_ORDER[declared]
            else declared
        )
        if effective is not spec.risk:
            logger.warning(
                "Plugin '%s' action '%s' registered as %s at runtime but "
                "declared %s in the manifest — raising to %s",
                self._manifest.name, spec.name, spec.risk.value,
                declared.value, effective.value,
            )
        namespaced = f"{self._manifest.name}.{spec.name}"
        if self._actions.get(namespaced) is not None or any(
            pending.name == namespaced for pending in self._pending_actions
        ):
            raise PluginError(
                f"plugin '{self._manifest.name}': action '{namespaced}' "
                "already registered"
            )
        self._pending_actions.append(ActionSpec(
            name=namespaced,
            description=f"[{self._manifest.name} plugin] {spec.description}",
            risk=effective,
            handler=spec.handler,
            validate=spec.validate,
        ))
        return namespaced

    def register_module(self, module: BaseModule) -> None:
        """Register a perception/etc. module; its name must carry the
        plugin's namespace prefix (``<plugin>_…``)."""
        if self._modules is None:
            raise PluginError(
                f"plugin '{self._manifest.name}': no module registry available"
            )
        prefix = f"{self._manifest.name}_"
        if not module.name.startswith(prefix):
            raise PluginError(
                f"plugin '{self._manifest.name}': module name "
                f"'{module.name}' must start with '{prefix}'"
            )
        if module.name in self._modules.names or any(
            pending.name == module.name for pending in self._pending_modules
        ):
            raise PluginError(
                f"plugin '{self._manifest.name}': module '{module.name}' "
                "already registered"
            )
        self._pending_modules.append(module)

    # ------------------------------------------------------------------
    def _commit(self) -> None:
        """Loader-only: apply staged capabilities after a clean register()."""
        for spec in self._pending_actions:
            self._actions.register(spec)
            self._report.actions.append(spec.name)
        for module in self._pending_modules:
            self._modules.register(module)
            self._report.modules.append(module.name)


# ---------------------------------------------------------------------------
def discover_plugin_directories(paths: list[str]) -> list[Path]:
    """Directories containing ``plugin.yaml`` under the configured paths.

    Each configured path may itself be a plugin (has ``plugin.yaml``) or a
    directory *of* plugins (one level deep). Sorted for deterministic load
    order.
    """
    found: list[Path] = []
    for raw in paths:
        base = Path(raw).expanduser()
        if not base.is_dir():
            logger.warning("plugins.paths entry is not a directory: %s", base)
            continue
        if (base / MANIFEST_FILENAME).is_file():
            found.append(base.resolve())
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / MANIFEST_FILENAME).is_file():
                found.append(child.resolve())
    return found


def _import_entry(manifest: PluginManifest):
    module_name = f"dtwin_plugin_{manifest.name}"
    spec = importlib.util.spec_from_file_location(module_name, manifest.entry)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot import entry file {manifest.entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_plugins(
    config: PluginsConfig,
    action_registry: ActionRegistry | None,
    module_registry: ModuleRegistry | None,
    secrets=None,  # SecretStore | None — exposed to plugins via api.secret()
) -> list[LoadedPlugin]:
    """Discover and load every configured plugin, isolating failures."""
    reports: list[LoadedPlugin] = []
    if not config.enabled or not config.paths:
        return reports
    seen: set[str] = set()
    for directory in discover_plugin_directories(list(config.paths)):
        report = LoadedPlugin(name=directory.name, directory=directory)
        reports.append(report)
        try:
            manifest = PluginManifest.from_directory(directory)
            report.name = manifest.name
            report.version = manifest.version
            if manifest.name in seen:
                raise PluginError(
                    f"duplicate plugin name '{manifest.name}' — skipped"
                )
            if manifest.isolation == "subprocess":
                from digital_twin.plugins.sandbox import SandboxedPlugin

                if action_registry is None:
                    raise PluginError(
                        f"plugin '{manifest.name}': automation is disabled "
                        "— no actions can be registered")
                sandbox = SandboxedPlugin(manifest)
                specs = sandbox.proxy_specs()
                for spec in specs:  # pre-check, then commit (two-phase)
                    if action_registry.get(spec.name) is not None:
                        sandbox.close()
                        raise PluginError(
                            f"action '{spec.name}' already registered")
                for spec in specs:
                    action_registry.register(spec)
                    report.actions.append(spec.name)
                report.sandboxed = True
                seen.add(manifest.name)
                logger.info(
                    "Loaded SANDBOXED plugin %s %s (actions: %s)",
                    manifest.name, manifest.version, report.actions or "-")
                continue
            entry_module = _import_entry(manifest)
            register = getattr(entry_module, "register", None)
            if not callable(register):
                raise PluginError(
                    f"entry file {manifest.entry.name} does not define "
                    "register(api)"
                )
            api = PluginAPI(manifest, action_registry, module_registry,
                            report, secrets=secrets)
            register(api)
            api._commit()  # nothing lands unless register() succeeded fully
            seen.add(manifest.name)
            logger.info(
                "Loaded plugin %s %s (actions: %s; modules: %s)",
                manifest.name, manifest.version,
                report.actions or "-", report.modules or "-",
            )
        except (PluginManifestError, PluginError) as exc:
            report.error = str(exc)
            logger.error("Plugin %s disabled: %s", report.name, exc)
        except BaseException as exc:  # fault isolation: a plugin cannot
            report.error = f"{type(exc).__name__}: {exc}"  # take the kernel down
            logger.error("Plugin %s crashed during load: %s", report.name,
                         report.error)
    return reports
