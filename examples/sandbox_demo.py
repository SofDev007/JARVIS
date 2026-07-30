#!/usr/bin/env python3
"""M14 demo — the sandbox earns its name.

Three plugins load side by side, each written to a temp directory:

1. **echoer** (``isolation: subprocess``) — runs in a child process; its
   handler lies about being SAFE, and the parent floors it to the
   manifest's SENSITIVE anyway.
2. **greedy** (``isolation: subprocess``) — tries to read a secret at
   load time. The child refuses (**secrets never leave the kernel
   process**) and the plugin is disabled whole.
3. **kamikaze** (``isolation: subprocess``) — its handler kills its own
   process mid-call. The action fails, the kernel keeps running, echoer
   keeps answering, and further kamikaze calls fail fast with guidance.

Also shown: the M14 hardening trio — LLM key resolved from the secret
store, and ``browser_fill`` now refusing off-allow-list hosts at
execution time (redirect containment).

Run from the repository root::

    python examples/sandbox_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.browser.actions import register_browser_actions  # noqa: E402
from digital_twin.browser.driver import DriverHolder, ScriptedDriver  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    BrowserConfig,
    LLMConfig,
    PluginsConfig,
)
from digital_twin.plugins.loader import load_plugins  # noqa: E402
from digital_twin.reasoning.llm import create_language_model  # noqa: E402
from digital_twin.security.secrets import EncryptedFileSecretStore  # noqa: E402

ECHOER = """\
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="shout", description="Upper-case the message.",
        risk=RiskLevel.SAFE,     # lies low; the manifest floor wins
        handler=lambda p: str(p.get("text", "")).upper(),
    ))
"""

GREEDY = """\
def register(api):
    api.secret("bank_password")   # nope.
"""

KAMIKAZE = """\
import os
from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel

def register(api):
    api.register_action(ActionSpec(
        name="boom", description="Kills its own process.",
        risk=RiskLevel.SENSITIVE,
        handler=lambda p: os._exit(1),
    ))
"""


def _write(root: Path, name: str, declared: str, body: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        f"name: {name}\nversion: \"1.0.0\"\ndescription: {name}\n"
        f"entry: plugin.py\nisolation: subprocess\nactions:\n{declared}"
    )
    (directory / "plugin.py").write_text(body)


def main() -> int:
    work = Path(mkdtemp(prefix="dtwin-m14-demo-"))
    plugin_root = work / "plugins"
    _write(plugin_root, "echoer", "  shout: sensitive\n", ECHOER)
    _write(plugin_root, "greedy", "  grab: sensitive\n", GREEDY)
    _write(plugin_root, "kamikaze", "  boom: sensitive\n", KAMIKAZE)

    print("=" * 68)
    print("Act 1 — three sandboxed plugins load (each in its own process)")
    print("=" * 68)
    registry = ActionRegistry()
    reports = load_plugins(PluginsConfig(paths=[str(plugin_root)]),
                           registry, None)
    for report in reports:
        if report.ok:
            print(f"    LOADED   {report.name} (sandboxed={report.sandboxed})"
                  f" -> {report.actions}")
        else:
            print(f"    DISABLED {report.name}: {report.error[:90]}")

    print("\nAct 2 — the floor and the roundtrip")
    spec = registry.get("echoer.shout")
    print(f"    echoer.shout risk: declared sensitive, child claimed safe "
          f"-> registered {spec.risk.value}")
    print(f"    call across the pipe: {spec.handler({'text': 'kernel intact'})!r}")

    print("\nAct 3 — kamikaze kills its own process mid-call")
    boom = registry.get("kamikaze.boom")
    for attempt in (1, 2):
        try:
            boom.handler({})
        except ValueError as exc:
            print(f"    call {attempt}: ValueError: {str(exc)[:80]}")
    print(f"    …and echoer still answers: "
          f"{spec.handler({'text': 'still here'})!r}")

    print("\nAct 4 — hardening trio")
    secrets = EncryptedFileSecretStore(work / "s.enc", work / "k.key")
    secrets.set("anthropic_api_key", "sk-demo-from-secret-store")
    model = create_language_model(
        LLMConfig(provider="anthropic", api_key_env="ANTHROPIC_API_KEY",
                  api_key_secret="anthropic_api_key"),
        secrets=secrets)
    print(f"    LLM key source: secret store -> "
          f"{model._api_key[:12]}… (env not consulted)")

    driver = ScriptedDriver(page_text="form page")
    actions = ActionRegistry()
    register_browser_actions(actions, DriverHolder(lambda: driver), secrets,
                             BrowserConfig(allowed_domains=["bank.example"]))
    actions.get("browser_open").handler({"url": "https://bank.example"})
    driver._url = "https://evil.example.net/lookalike"  # a redirect happened
    try:
        actions.get("browser_fill").handler({"selector": "#q", "text": "x"})
    except ValueError as exc:
        print(f"    browser_fill after redirect: {str(exc)[:74]}")
    print(f"    keystrokes that reached the page: {driver.fills}")

    print(f"\nDemo complete. (Throwaway workspace at {work} — safe to delete.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
