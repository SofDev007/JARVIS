"""The air-board module: a normal :class:`BaseModule` that owns an
:class:`~digital_twin.airboard.server.AirboardServer` for its lifetime.
Nothing on the bus — the board's contract is HTTP (the tracker page and
the agent-facing scripts), not events."""

from __future__ import annotations

import logging

from digital_twin.airboard.orbs import load_orbs
from digital_twin.airboard.server import AirboardServer
from digital_twin.configuration.settings import AirboardConfig
from digital_twin.core.module import BaseModule

logger = logging.getLogger(__name__)


class AirboardModule(BaseModule):
    """Serve the gesture-controlled overlay board for this kernel."""

    name = "airboard"
    topics = ()

    def __init__(self, config: AirboardConfig):
        super().__init__()
        self._config = config
        self._server: AirboardServer | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("airboard is not running")
        return self._server.port

    def _on_start(self) -> None:
        orbs = load_orbs(self._config.orbs_file)
        self._server = AirboardServer(
            self._config.host,
            self._config.port,
            name=self._config.name,
            orbs=orbs,
            media_dir=self._config.media_dir,
            state_dir=self._config.state_dir,
            state_timeout_s=self._config.state_timeout_s,
        )
        self._server.start()

    def _on_stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None

    def _on_pause(self) -> None:
        self._on_stop()

    def _on_resume(self) -> None:
        self._on_start()
