"""Tests for the module lifecycle state machine and the registry."""

from __future__ import annotations

import pytest

from digital_twin.core.bus import EventBus
from digital_twin.core.events import Topics
from digital_twin.core.module import BaseModule, InvalidStateError, ModuleState
from digital_twin.core.registry import ModuleRegistry


class Recorder(BaseModule):
    name = "recorder"
    topics = ("test.topic",)

    def __init__(self, fail_on_start: bool = False):
        super().__init__()
        self.calls: list[str] = []
        self._fail_on_start = fail_on_start

    def _on_start(self) -> None:
        self.calls.append("start")
        if self._fail_on_start:
            raise RuntimeError("hardware missing")

    def _on_stop(self) -> None:
        self.calls.append("stop")

    def _metrics(self):
        return {"calls": len(self.calls)}


class Second(Recorder):
    name = "second"


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


# ---------------------------------------------------------------------------
# BaseModule state machine
# ---------------------------------------------------------------------------
def test_lifecycle_happy_path(bus):
    module = Recorder()
    assert module.state is ModuleState.CREATED
    module.start(bus)
    assert module.state is ModuleState.RUNNING and module.is_active
    module.pause()
    assert module.state is ModuleState.PAUSED
    module.resume()
    assert module.state is ModuleState.RUNNING
    module.stop()
    assert module.state is ModuleState.STOPPED
    # Default pause/resume delegate to stop/start hooks.
    assert module.calls == ["start", "stop", "start", "stop"]


def test_invalid_transitions_raise(bus):
    module = Recorder()
    with pytest.raises(InvalidStateError):
        module.pause()  # not running yet
    with pytest.raises(InvalidStateError):
        module.resume()  # never started
    module.start(bus)
    with pytest.raises(InvalidStateError):
        module.start(bus)  # already running
    module.resume()  # enable-while-enabled is an idempotent no-op
    assert module.state is ModuleState.RUNNING
    module.stop()
    module.stop()  # idempotent, no raise
    with pytest.raises(InvalidStateError):
        module.resume()  # stopped modules must be started, not resumed


def test_start_failure_marks_failed(bus):
    module = Recorder(fail_on_start=True)
    with pytest.raises(RuntimeError):
        module.start(bus)
    status = module.status()
    assert status.state is ModuleState.FAILED
    assert "hardware missing" in status.detail


def test_module_requires_name():
    class Nameless(BaseModule):
        pass

    with pytest.raises(ValueError):
        Nameless()


def test_status_reports_metrics(bus):
    module = Recorder()
    module.start(bus)
    assert module.status().metrics == {"calls": 1}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_registry_start_isolation_and_announcements(bus):
    lifecycle_events = []
    bus.subscribe(Topics.MODULE, lifecycle_events.append)

    registry = ModuleRegistry(bus)
    failing = Recorder(fail_on_start=True)
    healthy = Second()
    registry.register(failing)
    registry.register(healthy)

    registry.start_all()  # must not raise despite the failure
    assert bus.flush(timeout=2.0)

    assert failing.state is ModuleState.FAILED
    assert healthy.state is ModuleState.RUNNING

    by_module = {e.payload["name"]: e.payload for e in lifecycle_events}
    assert by_module["recorder"]["state"] == "failed"
    assert "hardware missing" in by_module["recorder"]["detail"]
    assert by_module["second"]["state"] == "running"


def test_registry_duplicate_names_rejected(bus):
    registry = ModuleRegistry(bus)
    registry.register(Recorder())
    with pytest.raises(ValueError):
        registry.register(Recorder())


def test_registry_enable_disable_and_statuses(bus):
    registry = ModuleRegistry(bus)
    module = Recorder()
    registry.register(module)
    registry.start_all()

    registry.disable("recorder")
    assert module.state is ModuleState.PAUSED
    registry.enable("recorder")
    assert module.state is ModuleState.RUNNING

    (status,) = registry.statuses()
    assert status.name == "recorder" and status.state is ModuleState.RUNNING

    with pytest.raises(KeyError):
        registry.disable("nope")


def test_registry_stop_all_reverse_order(bus):
    order: list[str] = []

    class Tracked(BaseModule):
        def __init__(self, name: str):
            self.name = name
            super().__init__()

        def _on_stop(self) -> None:
            order.append(self.name)

    registry = ModuleRegistry(bus)
    registry.register(Tracked("a"))
    registry.register(Tracked("b"))
    registry.start_all()
    registry.stop_all()
    assert order == ["b", "a"]
