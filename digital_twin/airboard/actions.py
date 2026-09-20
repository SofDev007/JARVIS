"""The ``open_airboard`` action: "Jarvis, open the airboard".

Hands-free is the whole point, so this is SAFE rather than SENSITIVE like
the general ``open_url``: it takes no parameters and can only ever open
*this* board's own loopback URL — there is no attacker-chosen destination,
and the page still has to ask the browser for the camera itself.

It also resumes a paused board, so "open airboard" works after "pause
airboard" without the user having to think about module lifecycles.
"""

from __future__ import annotations

import logging
import webbrowser
from typing import Any, Callable, Mapping

from digital_twin.airboard.module import AirboardModule
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.core.module import ModuleState
from digital_twin.security.permissions import RiskLevel

logger = logging.getLogger(__name__)


def register_airboard_actions(
    registry: ActionRegistry,
    module: AirboardModule,
    opener: Callable[[str], bool] = webbrowser.open,
) -> None:
    """Register board actions onto ``registry``."""

    def handle_open(params: Mapping[str, Any]) -> str:
        if module.state is ModuleState.PAUSED:
            module.resume()
        if not module.is_active:
            return f"the air board is not running ({module.state.value})"
        url = module.url
        if not opener(url):
            return f"no browser available for {url}"
        return f"opened the air board at {url}"

    registry.register(ActionSpec(
        name="open_airboard",
        description=("Open the air board (hand-gesture overlay) in the "
                     "browser; starts it if it was paused."),
        risk=RiskLevel.SAFE,
        handler=handle_open,
    ))
