"""The ``read_screen`` action — the only door to the user's screen.

Registered exactly like M9's ``speak``: a module capability exposed as a
normal action, which buys the whole security pipeline for free. The risk
level is the design:

* **SENSITIVE** — confirmed by default. Reading the screen exposes
  whatever is visible (passwords, private messages), so a human approves
  each capture unless they explicitly configure
  ``security.permissions: {read_screen: allow}``.
* The handler returns only character counts; the OCR text travels on
  ``perception.screen``, never through action results or the audit log.
* Users can remove the capability entirely with one rule:
  ``security.permissions: {read_screen: deny}``.
"""

from __future__ import annotations

from typing import Any, Mapping

from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.perception.screen.module import ScreenReadingModule
from digital_twin.security.permissions import RiskLevel


def register_screen_actions(
    registry: ActionRegistry, reader: ScreenReadingModule
) -> None:
    """Register screen-reading actions onto ``registry``."""

    def validate_read_screen(params: Mapping[str, Any]) -> None:
        if params:
            raise ValueError("read_screen takes no parameters")

    def handle_read_screen(params: Mapping[str, Any]) -> str:
        return reader.read_now()

    registry.register(ActionSpec(
        name="read_screen",
        description=("Capture the screen once, OCR it locally and publish "
                     "the visible text for reasoning (perception.screen)."),
        risk=RiskLevel.SENSITIVE,
        handler=handle_read_screen,
        validate=validate_read_screen,
    ))
