"""Tests for browser automation: URL policy, risk taxonomy, the
DANGEROUS clamp on interaction, and the secret-by-name discipline
(values never in params/results/audit)."""

from __future__ import annotations

import time

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry
from digital_twin.browser.actions import check_url, register_browser_actions
from digital_twin.browser.driver import (
    BrowserError,
    DriverHolder,
    PlaywrightDriver,
    ScriptedDriver,
)
from digital_twin.configuration.settings import (
    AutomationConfig,
    BrowserConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy, RiskLevel
from digital_twin.security.secrets import EncryptedFileSecretStore


# ---------------------------------------------------------------------------
# URL policy
# ---------------------------------------------------------------------------
def test_check_url_scheme_and_host():
    assert check_url("https://example.com/a", ()) == "https://example.com/a"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.com",
                "https://", "", None, "not a url"):
        with pytest.raises(ValueError):
            check_url(bad, ())


def test_check_url_domain_allow_list():
    allowed = ("github.com", "example.org")
    assert check_url("https://github.com/x", allowed)
    assert check_url("https://api.github.com/x", allowed)  # subdomain ok
    with pytest.raises(ValueError, match="allowed_domains"):
        check_url("https://evil-github.com/x", allowed)  # not a subdomain
    with pytest.raises(ValueError):
        check_url("https://github.com.evil.net/x", allowed)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


class _Setup:
    def __init__(self, bus, tmp_path, *, page_text="Hello page",
                 allowed_domains=(), answers=None, permissions=None):
        self.driver = ScriptedDriver(page_text=page_text, title="T")
        self.holder = DriverHolder(lambda: self.driver)
        self.secrets = EncryptedFileSecretStore(
            tmp_path / "s.enc", tmp_path / "k.key"
        )
        self.registry = ActionRegistry()
        register_browser_actions(
            self.registry, self.holder, self.secrets,
            BrowserConfig(allowed_domains=list(allowed_domains),
                          max_extract_chars=50),
        )
        self.confirmation = ScriptedConfirmation(answers or [])
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        security = SecurityConfig(permissions=permissions or {})
        self.dispatcher = ActionDispatcher(
            config=AutomationConfig(action_timeout_s=5.0),
            registry=self.registry,
            policy=PermissionPolicy(security.risk_defaults,
                                    security.permissions),
            confirmation=self.confirmation,
            audit=self.audit,
        )
        self.results = []
        bus.subscribe(Topics.ACTION_RESULT, self.results.append)

    def run(self, bus, action, params, count=1):
        self.dispatcher.start(bus)
        try:
            bus.publish(Event(Topics.ACTION_EXECUTE, "test",
                              {"action": action, "params": params}))
            deadline = time.time() + 3.0
            while len(self.results) < count and time.time() < deadline:
                time.sleep(0.01)
        finally:
            self.dispatcher.stop()
        return self.results


def test_risk_taxonomy(bus, tmp_path):
    setup = _Setup(bus, tmp_path)
    expected = {
        "browser_open": RiskLevel.SENSITIVE,
        "browser_extract_text": RiskLevel.SENSITIVE,
        "browser_fill": RiskLevel.DANGEROUS,
        "browser_click": RiskLevel.DANGEROUS,
        "browser_fill_secret": RiskLevel.DANGEROUS,
        "browser_close": RiskLevel.SAFE,
    }
    assert set(setup.registry.names) == set(expected)
    for name, risk in expected.items():
        assert setup.registry.get(name).risk is risk


def test_open_and_bounded_extract(bus, tmp_path):
    setup = _Setup(bus, tmp_path, page_text="X" * 500,
                   answers=[True, True])
    results = setup.run(bus, "browser_open",
                        {"url": "https://example.com"}, count=1)
    assert results[0].payload["status"] == "completed"
    assert setup.driver.opens == ["https://example.com"]

    bus2_results = setup.run(bus, "browser_extract_text",
                             {"max_chars": 10_000}, count=2)
    detail = bus2_results[1].payload["detail"]
    assert "(50 chars)" in detail  # requested 10k, config caps at 50


def test_extract_requires_an_open_page(bus, tmp_path):
    setup = _Setup(bus, tmp_path, answers=[True])
    results = setup.run(bus, "browser_extract_text", {})
    assert results[0].payload["status"] == "failed"
    assert "browser_open" in results[0].payload["detail"]


def test_click_dangerous_clamp_confirms_despite_allow(bus, tmp_path):
    setup = _Setup(bus, tmp_path, answers=[True],
                   permissions={"browser_click": "allow"})
    results = setup.run(bus, "browser_click", {"selector": "#buy-now"})
    assert setup.confirmation.requests  # allow was clamped to confirm
    assert results[0].payload["status"] == "completed"
    assert setup.driver.clicks == ["#buy-now"]


def test_open_outside_allow_list_is_invalid(bus, tmp_path):
    setup = _Setup(bus, tmp_path, allowed_domains=("github.com",),
                   answers=[True])
    results = setup.run(bus, "browser_open", {"url": "https://evil.net"})
    assert results[0].payload["status"] == "invalid"
    assert setup.confirmation.requests == []  # rejected before any gate
    assert setup.driver.opens == []


def test_fill_secret_value_never_leaks(bus, tmp_path):
    setup = _Setup(bus, tmp_path, allowed_domains=("bank.example",),
                   answers=[True, True],
                   permissions={"browser_fill_secret": "allow"})
    setup.secrets.set("bank_pw", "hunter2-SUPER-SECRET")
    setup.run(bus, "browser_open", {"url": "https://login.bank.example"},
              count=1)
    results = setup.run(bus, "browser_fill_secret",
                        {"selector": "#password", "secret": "bank_pw"},
                        count=2)
    payload = results[1].payload
    assert payload["status"] == "completed"
    # The value reached the page…
    assert setup.driver.fills == [("#password", "hunter2-SUPER-SECRET")]
    # …but never the result, the audit log, or the confirmation params.
    assert "hunter2" not in str(payload)
    for entry in setup.audit.tail(20):
        assert "hunter2" not in str(entry)
    for _, params in setup.confirmation.requests:
        assert "hunter2" not in str(params)
    # And the DANGEROUS clamp fired despite configured allow.
    assert any(action == "browser_fill_secret"
               for action, _ in setup.confirmation.requests)


def test_fill_secret_refuses_without_allow_list(bus, tmp_path):
    setup = _Setup(bus, tmp_path, answers=[True])  # no allowed_domains
    setup.secrets.set("pw", "value")
    results = setup.run(bus, "browser_fill_secret",
                        {"selector": "#p", "secret": "pw"})
    assert results[0].payload["status"] == "invalid"
    assert "allowed_domains" in results[0].payload["detail"]
    assert setup.driver.fills == []


def test_fill_secret_refuses_on_non_allowed_host(bus, tmp_path):
    setup = _Setup(bus, tmp_path, allowed_domains=("bank.example",),
                   answers=[True, True],
                   permissions={"browser_fill_secret": "confirm"})
    setup.secrets.set("pw", "value")
    # Note: opening evil.net is *invalid* under the allow-list, so simulate
    # a redirect by planting the URL directly on the scripted driver.
    setup.driver.open("https://evil.net/login", 1.0)
    results = setup.run(bus, "browser_fill_secret",
                        {"selector": "#p", "secret": "pw"})
    assert results[0].payload["status"] == "failed"
    assert "refusing to fill secret" in results[0].payload["detail"]
    assert setup.driver.fills == []


def test_missing_secret_fails_cleanly(bus, tmp_path):
    setup = _Setup(bus, tmp_path, allowed_domains=("bank.example",),
                   answers=[True, True],
                   permissions={"browser_fill_secret": "confirm"})
    setup.run(bus, "browser_open", {"url": "https://bank.example"}, count=1)
    results = setup.run(bus, "browser_fill_secret",
                        {"selector": "#p", "secret": "ghost"}, count=2)
    assert results[1].payload["status"] == "failed"
    assert "no such secret" in results[1].payload["detail"]


def test_browser_close_is_safe_and_idempotent(bus, tmp_path):
    setup = _Setup(bus, tmp_path, answers=[True])
    setup.run(bus, "browser_open", {"url": "https://example.com"}, count=1)
    # SAFE: no confirmation answers queued, still executes.
    results = setup.run(bus, "browser_close", {}, count=2)
    assert results[1].payload["status"] == "completed"
    assert setup.driver.closed is True
    results = setup.run(bus, "browser_close", {}, count=3)
    assert "was not open" in results[2].payload["detail"]


def test_playwright_missing_gives_install_guidance(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def blocking(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking)
    with pytest.raises(BrowserError, match="pip install playwright"):
        PlaywrightDriver()


def test_selector_validation(bus, tmp_path):
    setup = _Setup(bus, tmp_path)
    for bad in ({"selector": ""}, {"selector": "x" * 301}, {}):
        with pytest.raises(ValueError):
            setup.registry.get("browser_click").validate(bad)


# ---------------------------------------------------------------------------
# M14 hardening: execution-time host re-check for fill/click
# ---------------------------------------------------------------------------
def test_fill_and_click_recheck_host_when_allow_list_configured(bus, tmp_path):
    setup = _Setup(bus, tmp_path, allowed_domains=("bank.example",),
                   answers=[True, True],
                   permissions={"browser_fill": "confirm",
                                "browser_click": "confirm"})
    # Simulate a redirect landing off the allow-list after validation.
    setup.driver.open("https://evil.example.net/form", 1.0)
    results = setup.run(bus, "browser_fill",
                        {"selector": "#q", "text": "hello"})
    assert results[0].payload["status"] == "failed"
    assert "refusing browser_fill" in results[0].payload["detail"]
    assert setup.driver.fills == []
    results = setup.run(bus, "browser_click", {"selector": "#go"}, count=2)
    assert results[1].payload["status"] == "failed"
    assert setup.driver.clicks == []


def test_fill_without_allow_list_still_works_anywhere(bus, tmp_path):
    setup = _Setup(bus, tmp_path, answers=[True, True],
                   permissions={"browser_fill": "confirm"})
    setup.run(bus, "browser_open", {"url": "https://anywhere.net"}, count=1)
    results = setup.run(bus, "browser_fill",
                        {"selector": "#q", "text": "ok"}, count=2)
    assert results[1].payload["status"] == "completed"
    assert setup.driver.fills == [("#q", "ok")]
