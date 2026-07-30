"""Tests for the shipped connector plugins: all three load through the
real loader, calendar parses .ics, tasks roundtrip, and the email
connector resolves its password by name without ever leaking it."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from digital_twin.automation.registry import ActionRegistry
from digital_twin.configuration.settings import PluginsConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.registry import ModuleRegistry
from digital_twin.plugins.loader import load_plugins

CONNECTORS = Path(__file__).resolve().parent.parent / "plugins" / "examples"


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


class _FakeSecrets:
    def __init__(self, values):
        self._values = values

    def get(self, name):
        from digital_twin.security.secrets import SecretsError

        if name not in self._values:
            raise SecretsError(f"no such secret: '{name}'")
        return self._values[name]


def _load_all(bus, secrets=None):
    actions = ActionRegistry()
    reports = load_plugins(
        PluginsConfig(paths=[str(CONNECTORS)]),
        actions, ModuleRegistry(bus), secrets=secrets,
    )
    return actions, {report.name: report for report in reports}


def test_all_three_connectors_load_cleanly(bus):
    actions, reports = _load_all(bus)
    assert all(report.ok for report in reports.values()), reports
    assert set(reports) == {"calendar", "tasks", "email"}
    assert set(actions.names) == {
        "calendar.upcoming_events", "calendar.create_event",
        "tasks.add_task", "tasks.list_tasks", "tasks.complete_task",
        "email.check_email", "email.send_email",
    }


def test_calendar_parses_ics_and_windows_events(bus, tmp_path,
                                                monkeypatch):
    soon = datetime.now() + timedelta(days=2)
    far = datetime.now() + timedelta(days=90)
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{soon:%Y%m%dT%H%M%S}\r\n"
        "SUMMARY:Sprint demo with\r\n"
        " Sangeetha\r\n"  # folded line
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART;TZID=Asia/Kolkata:{far:%Y%m%dT%H%M%S}\r\n"
        "SUMMARY:Far future review\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    calendar_dir = tmp_path / "cals"
    calendar_dir.mkdir()
    (calendar_dir / "work.ics").write_text(ics)

    # Point the plugin's config at our directory via a patched manifest dir.
    actions, _ = _load_all(bus)
    spec = actions.get("calendar.upcoming_events")
    monkeypatch.chdir(tmp_path)  # relative default won't exist anyway
    # Easier: call handler with patched module-level config through params —
    # the plugin reads its config at register time, so patch by re-loading
    # with a manifest copy would be heavy. Instead exercise the parser and
    # the handler error path, then a real run against the default location.
    from importlib import util as importlib_util

    module_spec = importlib_util.spec_from_file_location(
        "calendar_plugin_direct", CONNECTORS / "calendar_ics" / "plugin.py")
    module = importlib_util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    events = module.parse_ics_events(ics)
    assert len(events) == 2
    assert events[0]["summary"] == "Sprint demo withSangeetha"

    class API:
        config = {"ics_dir": str(calendar_dir)}

        def register_action(self, action_spec):
            self.specs[action_spec.name] = action_spec

    api = API()
    api.specs = {}
    module.register(api)
    detail = api.specs["upcoming_events"].handler({"days": 7})
    assert "Sprint demo" in detail
    assert "Far future" not in detail  # outside the window
    with pytest.raises(ValueError):
        api.specs["upcoming_events"].validate({"days": 0})
    assert spec is not None  # loader-registered variant exists too


def test_tasks_roundtrip(bus, tmp_path):
    from importlib import util as importlib_util

    module_spec = importlib_util.spec_from_file_location(
        "tasks_plugin_direct", CONNECTORS / "tasks_local" / "plugin.py")
    module = importlib_util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    class API:
        config = {"tasks_file": str(tmp_path / "tasks.json")}
        specs = {}

        def register_action(self, action_spec):
            self.specs[action_spec.name] = action_spec

    api = API()
    module.register(api)
    assert "added task 1" in api.specs["add_task"].handler(
        {"text": "ship M13"})
    api.specs["add_task"].handler({"text": "write report"})
    listing = api.specs["list_tasks"].handler({})
    assert "2 open task(s)" in listing and "ship M13" in listing
    assert "completed task #1" in api.specs["complete_task"].handler(
        {"number": 1})
    assert "1 open task(s)" in api.specs["list_tasks"].handler({})
    with pytest.raises(ValueError):
        api.specs["complete_task"].handler({"number": 99})


def test_email_connector_headers_only_and_no_password_leak(bus, tmp_path,
                                                           monkeypatch):
    calls = {}

    class FakeIMAP:
        def __init__(self, host):
            calls["host"] = host

        def login(self, user, password):
            calls["user"] = user
            calls["password"] = password

        def select(self, mailbox, readonly=False):
            calls["mailbox"] = mailbox
            calls["readonly"] = readonly

        def search(self, charset, criterion):
            calls["criterion"] = criterion
            return "OK", [b"1 2 3"]

        def fetch(self, message_id, parts):
            calls.setdefault("fetched", []).append(parts)
            return "OK", [(b"1", b"From: boss@example.com\r\n"
                                 b"Subject: Q3 numbers\r\n\r\n")]

        def logout(self):
            calls["logout"] = True

    fake_module = types.ModuleType("imaplib")
    fake_module.IMAP4_SSL = FakeIMAP
    monkeypatch.setitem(sys.modules, "imaplib", fake_module)

    from importlib import util as importlib_util

    module_spec = importlib_util.spec_from_file_location(
        "email_plugin_direct", CONNECTORS / "email_imap" / "plugin.py")
    module = importlib_util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    class API:
        config = {"host": "imap.example.com", "username": "vansh@x.com",
                  "password_secret": "email_password", "mailbox": "INBOX",
                  "limit": 2}

        def __init__(self, secrets):
            self._secrets = secrets

        def secret(self, name):
            return self._secrets.get(name)

        def register_action(self, action_spec):
            self.specs[action_spec.name] = action_spec

    api = API(_FakeSecrets({"email_password": "hunter2-IMAP"}))
    api.specs = {}
    module.register(api)
    detail = api.specs["check_email"].handler({})
    assert "3 unread" in detail and "Q3 numbers" in detail
    assert calls["password"] == "hunter2-IMAP"  # reached the server…
    assert "hunter2" not in detail              # …never the result
    assert calls["readonly"] is True            # mailbox never mutated
    assert all(b"PEEK" in part.encode() if isinstance(part, str)
               else True for part in calls["fetched"])
    assert calls["logout"] is True

    # Missing secret fails cleanly, naming the secret, not any value.
    api2 = API(_FakeSecrets({}))
    api2.specs = {}
    module.register(api2)
    with pytest.raises(Exception, match="email_password"):
        api2.specs["check_email"].handler({})
