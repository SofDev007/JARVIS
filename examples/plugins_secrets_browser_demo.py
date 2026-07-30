#!/usr/bin/env python3
"""M11 demo — a third-party plugin, a secret, and a login that never leaks.

No Playwright, no OS keyring needed: a scripted browser driver and a
temp-dir secret store stand in, but the plugin loader, the dispatcher,
the permission policy and the audit log are the real ones. Four acts:

1. **A plugin loads under contract** — a plugin written to a temp
   directory declares its one action in ``plugin.yaml``; the loader
   namespaces it (``greeter.wave``) and it runs through the normal gates.
2. **The contract is enforced** — a second plugin tries to register an
   action it never declared, and is disabled entirely (two-phase commit:
   nothing partial survives).
3. **Secrets by name** — a password is stored encrypted; the file on
   disk provably does not contain it.
4. **Login without leaking** — ``browser_fill_secret`` types the secret
   into an allow-listed page. It is DANGEROUS (clamped to confirmation
   even when configured ``allow``), and the audit log, action results and
   confirmation prompts contain the secret's *name* only — never its
   value. A non-allow-listed host is refused outright.

Run from the repository root::

    python examples/plugins_secrets_browser_demo.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.browser.actions import register_browser_actions  # noqa: E402
from digital_twin.browser.driver import DriverHolder, ScriptedDriver  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    BrowserConfig,
    PluginsConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.core.registry import ModuleRegistry  # noqa: E402
from digital_twin.plugins.loader import load_plugins  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ConfirmationProvider  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402
from digital_twin.security.secrets import EncryptedFileSecretStore  # noqa: E402

GREETER = """\
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="wave",
        description="Wave hello.",
        risk=RiskLevel.SENSITIVE,
        handler=lambda params: f"wave to {params.get('who', 'world')}",
    ))
"""

SNEAKY = """\
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="exfiltrate",           # never declared in plugin.yaml
        description="Definitely fine.",
        risk=RiskLevel.SAFE,
        handler=lambda params: "oops",
    ))
"""


class NarratingConfirmation(ConfirmationProvider):
    name = "narrating"

    def __init__(self) -> None:
        self.requests = []

    def request(self, action, params, timeout_s) -> bool:
        self.requests.append(action)
        print(f"    [confirm?] {action}({dict(params)}) -> approved")
        return True


def _write_plugin(root: Path, name: str, declared: str, body: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        f"name: {name}\nversion: \"1.0.0\"\n"
        f"description: {name} demo plugin\nentry: plugin.py\n"
        f"actions:\n{declared}"
    )
    (directory / "plugin.py").write_text(body)


def _await(predicate, timeout=3.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)


def main() -> int:
    work = Path(mkdtemp(prefix="dtwin-m11-demo-"))
    plugin_root = work / "plugins"
    _write_plugin(plugin_root, "greeter", "  wave: sensitive\n", GREETER)
    _write_plugin(plugin_root, "sneaky", "  wave: safe\n", SNEAKY)

    bus = EventBus()
    bus.start()
    actions = ActionRegistry()
    modules = ModuleRegistry(bus)

    print("=" * 68)
    print("Act 1+2 — plugin loading under the manifest contract")
    print("=" * 68)
    reports = load_plugins(PluginsConfig(paths=[str(plugin_root)]),
                           actions, modules)
    for report in reports:
        if report.ok:
            print(f"    LOADED   {report.name} {report.version} "
                  f"-> actions {report.actions}")
        else:
            print(f"    DISABLED {report.name}: {report.error}")
    print(f"    registry now: {actions.names}")

    # --- browser + secrets on the same dispatcher -----------------------
    driver = ScriptedDriver(page_text="Welcome to Example Bank", title="Bank")
    holder = DriverHolder(lambda: driver)
    secrets = EncryptedFileSecretStore(work / "secrets.enc",
                                       work / "secrets.key")
    register_browser_actions(
        actions, holder, secrets,
        BrowserConfig(allowed_domains=["bank.example"]),
    )

    confirmation = NarratingConfirmation()
    audit = AuditLog(work / "audit.jsonl")
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=5.0),
        registry=actions,
        policy=PermissionPolicy(
            risk_defaults={"safe": "allow", "sensitive": "confirm",
                           "dangerous": "deny"},
            overrides={"browser_fill_secret": "allow"},  # clamp will bite
        ),
        confirmation=confirmation,
        audit=audit,
    )
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)

    def run(action, params):
        before = len(results)
        bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                          {"action": action, "params": params}))
        _await(lambda: len(results) > before)
        payload = results[-1].payload
        print(f"    {action:22s} -> {payload['status']}: {payload['detail']}")
        return payload

    print("\n    the loaded plugin action runs through the normal gates:")
    run("greeter.wave", {"who": "Sangeetha"})

    print("\n" + "=" * 68)
    print("Act 3 — secrets: encrypted at rest, referenced by name")
    print("=" * 68)
    secrets.set("bank_password", "hunter2-SUPER-SECRET")
    on_disk = (work / "secrets.enc").read_bytes()
    print(f"    stored secret 'bank_password' "
          f"({len('hunter2-SUPER-SECRET')} chars)")
    print(f"    plaintext in secrets.enc: "
          f"{b'hunter2' in on_disk} (file is Fernet ciphertext)")

    print("\n" + "=" * 68)
    print("Act 4 — login without leaking (DANGEROUS + clamp + allow-list)")
    print("=" * 68)
    run("browser_open", {"url": "https://login.bank.example/signin"})
    payload = run("browser_fill_secret",
                  {"selector": "#password", "secret": "bank_password"})
    print(f"    driver actually received: "
          f"{driver.fills[-1][1][:9]}… (the real value, page-side only)")
    leaked = any(
        "hunter2" in str(entry) for entry in audit.tail(20)
    ) or "hunter2" in str(payload)
    print(f"    secret value anywhere in audit/results: {leaked}")

    print("\n    and against a host that is NOT allow-listed:")
    driver._url = "https://evil.example.net/phish"  # simulate a redirect
    run("browser_fill_secret",
        {"selector": "#password", "secret": "bank_password"})

    dispatcher.stop()
    bus.stop()
    print(f"\nDemo complete. (Throwaway workspace at {work} — safe to delete.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
