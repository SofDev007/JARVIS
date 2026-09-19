"""Run just the air board, without starting the rest of the kernel
(voice, gesture, dashboard, etc.) — useful for testing the board on its
own.

PowerShell::

    python -m digital_twin.airboard.run
"""

from __future__ import annotations

import argparse
import time

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
    server = AirboardServer.from_config(config)
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
