"""Calendar connector plugin: upcoming events from local .ics files.

Deliberately offline: point ``ics_dir`` at a folder of exported/synced
``.ics`` files and ``calendar.upcoming_events`` reads them fresh on every
call — no accounts, no OAuth, no background sync. The parser handles the
subset of RFC 5545 that calendar exports actually use (VEVENT blocks,
folded lines, ``DTSTART``/``SUMMARY`` with or without ``TZID``/``VALUE=DATE``
parameters); exotic recurrence rules are out of scope for a connector
example and noted as such.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from digital_twin.automation.registry import ActionSpec
from digital_twin.security.permissions import RiskLevel


def _unfold(lines: list[str]) -> list[str]:
    """RFC 5545 line unfolding: a leading space/tab continues the line."""
    unfolded: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line.rstrip("\r\n"))
    return unfolded


def _parse_dt(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_ics_events(text: str) -> list[dict[str, Any]]:
    """Extract ``{summary, start}`` dicts from one .ics document."""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _unfold(text.splitlines()):
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            current = {}
        elif upper.startswith("END:VEVENT"):
            if current and current.get("start"):
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, _, value = line.partition(":")
            key = key.split(";", 1)[0].upper()  # drop TZID=… parameters
            if key == "SUMMARY":
                current["summary"] = value.strip() or "(untitled)"
            elif key == "DTSTART":
                current["start"] = _parse_dt(value)
    return events


def register(api) -> None:
    ics_dir = Path(str(api.config.get("ics_dir", "data/calendars")))

    def validate(params: Mapping[str, Any]) -> None:
        days = params.get("days")
        if days is not None and (not isinstance(days, int)
                                 or not 1 <= days <= 365):
            raise ValueError("'days' must be an integer in 1..365")

    def handle(params: Mapping[str, Any]) -> str:
        days = int(params.get("days") or 7)
        directory = ics_dir.expanduser()
        if not directory.is_dir():
            raise ValueError(
                f"calendar directory not found: {directory} — set the "
                "plugin's ics_dir to a folder of .ics files"
            )
        now = datetime.now()
        horizon = now + timedelta(days=days)
        upcoming: list[tuple[datetime, str]] = []
        for ics_file in sorted(directory.glob("*.ics")):
            text = ics_file.read_text(encoding="utf-8", errors="replace")
            for event in parse_ics_events(text):
                start = event["start"]
                if now <= start <= horizon:
                    upcoming.append((start, event.get("summary",
                                                      "(untitled)")))
        if not upcoming:
            return f"no events in the next {days} day(s)"
        upcoming.sort()
        listing = "; ".join(
            f"{start:%a %d %b %H:%M} — {summary}"
            for start, summary in upcoming[:10]
        )
        return f"{len(upcoming)} event(s) in the next {days} day(s): {listing}"

    api.register_action(ActionSpec(
        name="upcoming_events",
        description="List upcoming events from local .ics calendar files.",
        risk=RiskLevel.SENSITIVE,
        handler=handle,
        validate=validate,
    ))

    # -- create_event (DANGEROUS: writes a calendar file others may sync) ----
    def validate_create(params: Mapping[str, Any]) -> None:
        summary = params.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("a non-empty 'summary' is required")
        if len(summary) > 300:
            raise ValueError("'summary' exceeds 300 characters")
        start = params.get("start")
        if not isinstance(start, str) or _parse_dt(start) is None:
            raise ValueError(
                "'start' must be YYYYMMDDTHHMMSS or YYYYMMDD (e.g. "
                "20260720T140000)")
        minutes = params.get("duration_minutes", 60)
        if not isinstance(minutes, int) or not 1 <= minutes <= 24 * 60:
            raise ValueError("'duration_minutes' must be 1..1440")

    def handle_create(params: Mapping[str, Any]) -> str:
        import uuid

        directory = Path(str(api.config.get("ics_dir", "data/calendars")))
        directory.mkdir(parents=True, exist_ok=True)
        start_dt = _parse_dt(str(params["start"]))
        minutes = int(params.get("duration_minutes", 60))
        end_dt = start_dt + timedelta(minutes=minutes)
        summary = str(params["summary"]).strip().replace("\n", " ")
        uid = f"{uuid.uuid4().hex}@digital-twin"
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "PRODID:-//Digital Twin//Connector//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:{stamp}\r\n"
            f"DTSTART:{start_dt:%Y%m%dT%H%M%S}\r\n"
            f"DTEND:{end_dt:%Y%m%dT%H%M%S}\r\n"
            f"SUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        out = directory / f"dtwin-{stamp}-{uid[:8]}.ics"
        out.write_text(ics, encoding="utf-8")
        return (f"created event '{summary}' at {start_dt:%a %d %b %H:%M} "
                f"({minutes} min) → {out.name}")

    api.register_action(ActionSpec(
        name="create_event",
        description=("Write a new event to a local .ics file (summary, "
                     "start, duration_minutes). Anything syncing the folder "
                     "will pick it up — always confirmed."),
        risk=RiskLevel.DANGEROUS,
        handler=handle_create,
        validate=validate_create,
    ))
