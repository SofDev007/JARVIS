"""``digital-twin-board`` — the agent's hands: POST one JSON command to the
air board's /cmd endpoint and print the HTTP status (204 accepted, 400
rejected). Ported from barehands' board.sh.

PowerShell::

    digital-twin-board '{"a": "clear"}'

Uses urllib (stdlib) rather than curl, so there's no interpreter/PATH
resolution problem to defend against in the first place — this runs inside
the same Python environment the package was installed into.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

from digital_twin.configuration.settings import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="digital-twin-board",
        description="POST one JSON command to the air board.")
    parser.add_argument("command", help='JSON command, e.g. \'{"a": "clear"}\'')
    parser.add_argument("--config", default=None,
                        help="Configuration file (defaults resolved as the "
                             "kernel does).")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    url = f"http://{config.airboard.host}:{config.airboard.port}/cmd"
    request = urllib.request.Request(
        url, data=args.command.encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            print(response.status)
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.code)
        return 0
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not reach {url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
