"""Run just the air board, without starting the rest of the kernel
(voice, gesture, dashboard, etc.) — useful for testing the board on its
own.

PowerShell::

    python -m digital_twin.airboard.run
"""

from __future__ import annotations

import argparse
import time

from digital_twin.airboard.orbs import load_orbs
from digital_twin.airboard.server import AirboardServer
from digital_twin.configuration.settings import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="digital-twin-airboard",
        description="Run only the air board's HTTP server.")
    parser.add_argument("--config", default=None,
                        help="Configuration file (defaults resolved as the "
                             "kernel does).")
    args = parser.parse_args(argv)

    config = load_config(args.config).airboard
    orbs = load_orbs(config.orbs_file)
    server = AirboardServer(
        config.host, config.port,
        name=config.name, orbs=orbs,
        media_dir=config.media_dir, state_dir=config.state_dir,
        state_timeout_s=config.state_timeout_s,
    )
    server.start()
    print(f"Air board listening on http://{config.host}:{server.port}/ "
          f"(Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
