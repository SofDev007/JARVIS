"""Built-in actions: the assistant's first, deliberately small toolbox.

Cross-platform and dependency-light on purpose. The interesting property is
not what these actions do but how they are guarded:

* ``log_message`` / ``notify`` are :data:`RiskLevel.SAFE` — worst case is a
  log line or a desktop toast.
* ``open_url`` is :data:`RiskLevel.SENSITIVE` and validates the scheme
  (http/https only — no ``file:``, no ``javascript:``).
* ``open_application`` is :data:`RiskLevel.SENSITIVE` and can only launch
  commands from the **configured allow-list** — there is deliberately no
  "run arbitrary command" action, and there won't be one.

Keyboard/mouse emulation (``next_slide`` and friends) needs an input-
synthesis dependency and its own risk analysis; that is a future milestone,
not a quick add.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.security.permissions import RiskLevel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# log_message
# ---------------------------------------------------------------------------
def _validate_log_message(params: Mapping[str, Any]) -> None:
    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("log_message requires a non-empty 'message' string")


def _log_message(params: Mapping[str, Any]) -> str:
    message = str(params["message"])
    logger.info("[action] %s", message)
    return message


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------
def _validate_notify(params: Mapping[str, Any]) -> None:
    for key in ("title", "message"):
        value = params.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"notify requires a non-empty {key!r} string")


def _notify(params: Mapping[str, Any]) -> str:
    """Best-effort desktop notification; falls back to the log (SAFE must
    never fail loudly just because a desktop helper is missing)."""
    title, message = str(params["title"]), str(params["message"])
    command: list[str] | None = None
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        command = ["notify-send", title, message]
    elif sys.platform == "darwin" and shutil.which("osascript"):
        script = f'display notification "{message}" with title "{title}"'
        command = ["osascript", "-e", script]
    if command is not None:
        try:
            subprocess.run(
                command, timeout=5, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return "notification shown"
        except (OSError, subprocess.SubprocessError):
            logger.warning("Desktop notification failed; falling back to log")
    logger.info("[notify] %s: %s", title, message)
    return "notification logged"


# ---------------------------------------------------------------------------
# open_url
# ---------------------------------------------------------------------------
def _validate_open_url(params: Mapping[str, Any]) -> None:
    url = params.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("open_url requires an http(s) 'url'")
    if any(ch.isspace() for ch in url):
        raise ValueError("open_url: url must not contain whitespace")


def _open_url(params: Mapping[str, Any]) -> str:
    url = str(params["url"])
    opened = webbrowser.open(url)
    return f"opened {url}" if opened else f"no browser available for {url}"


# ---------------------------------------------------------------------------
# open_application (allow-listed)
# ---------------------------------------------------------------------------
def _make_open_application(applications: Mapping[str, str]):
    allowed = dict(applications)

    def validate(params: Mapping[str, Any]) -> None:
        app = params.get("app")
        if not isinstance(app, str) or app not in allowed:
            raise ValueError(
                f"open_application: 'app' must be one of the configured "
                f"allow-list {sorted(allowed) or '(empty)'}"
            )

    def handler(params: Mapping[str, Any]) -> str:
        app = str(params["app"])
        command = shlex.split(allowed[app])
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # the app must outlive the assistant
        )
        return f"launched {app!r}"

    return validate, handler


# ---------------------------------------------------------------------------
# get_time
# ---------------------------------------------------------------------------
def _validate_get_time(params: Mapping[str, Any]) -> None:
    tz = params.get("timezone")
    if tz is None:
        return
    if not isinstance(tz, str) or not tz.strip():
        raise ValueError("get_time: 'timezone' must be a non-empty IANA "
                          "name, e.g. 'America/New_York', or omitted for local time")
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"get_time: unknown timezone {tz!r}") from exc


def _get_time(params: Mapping[str, Any]) -> str:
    tz = params.get("timezone")
    now = datetime.now(ZoneInfo(tz)) if tz else datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)")


# ---------------------------------------------------------------------------
def register_builtin_actions(
    registry: ActionRegistry, applications: Mapping[str, str] | None = None
) -> None:
    """Register the built-in toolbox onto ``registry``."""
    registry.register(
        ActionSpec(
            name="log_message",
            description="Write a message to the assistant log.",
            risk=RiskLevel.SAFE,
            handler=_log_message,
            validate=_validate_log_message,
        )
    )
    registry.register(
        ActionSpec(
            name="notify",
            description="Show a desktop notification (falls back to the log).",
            risk=RiskLevel.SAFE,
            handler=_notify,
            validate=_validate_notify,
        )
    )
    registry.register(
        ActionSpec(
            name="open_url",
            description="Open an http(s) URL in the default browser.",
            risk=RiskLevel.SENSITIVE,
            handler=_open_url,
            validate=_validate_open_url,
        )
    )
    registry.register(
        ActionSpec(
            name="get_time",
            description="Get the current date/time, optionally in an IANA "
                        "timezone (e.g. 'America/New_York'); local time if omitted.",
            risk=RiskLevel.SAFE,
            handler=_get_time,
            validate=_validate_get_time,
        )
    )
    validate_app, handle_app = _make_open_application(applications or {})
    registry.register(
        ActionSpec(
            name="open_application",
            description="Launch an application from the configured allow-list.",
            risk=RiskLevel.SENSITIVE,
            handler=handle_app,
            validate=validate_app,
        )
    )
