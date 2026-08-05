"""Device CLI: enrol, list and revoke devices from a terminal.

Identity is possession of an enrolled device. Enrolling generates a keypair and
a certificate; the private key is DPAPI-protected and never printed. Enrol and
revoke are written to the tamper-evident audit log.

PowerShell::

    python -m digital_twin.security.device_cli enroll --label "Arnav laptop"
    python -m digital_twin.security.device_cli list
    python -m digital_twin.security.device_cli revoke --id <device_id>
"""

from __future__ import annotations

import argparse
import sys

from digital_twin.configuration.settings import load_config
from digital_twin.security.audit import build_audit_log
from digital_twin.security.device_identity import (
    DeviceIdentityError,
    DeviceRegistry,
)


def build_parser() -> argparse.ArgumentParser:
    # --config lives on the subparsers (parents) only, never also on the top
    # parser: defining the same dest in both positions makes the subparser's
    # default clobber a value given before the subcommand. So flags follow the
    # subcommand, e.g. `enroll --label X --config Y`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="Configuration file (defaults resolved as the "
                             "kernel does).")

    parser = argparse.ArgumentParser(
        prog="device_cli",
        description="Enrol, list and revoke KNOWA devices.")
    commands = parser.add_subparsers(dest="command", required=True)

    enroll = commands.add_parser("enroll", parents=[common],
                                 help="Enrol a new device.")
    enroll.add_argument("--label", required=True,
                        help="Human label, e.g. 'Arnav laptop'.")
    commands.add_parser("list", parents=[common], help="List devices.")
    revoke = commands.add_parser("revoke", parents=[common],
                                 help="Revoke a device by id.")
    revoke.add_argument("--id", required=True, help="Device id to revoke.")
    return parser


def _registry(args) -> DeviceRegistry:
    config = load_config(args.config)
    audit = build_audit_log(config.security)
    return DeviceRegistry(config.security.devices_dir, audit=audit)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = _registry(args)
        if args.command == "enroll":
            device_id = registry.enroll(args.label)
            print(f"enrolled '{args.label}'\n  device_id: {device_id}")
            return 0
        if args.command == "list":
            devices = registry.list()
            if not devices:
                print("(no devices enrolled)")
                return 0
            for device in devices:
                print(f"{device['device_id']}  {device['status']:8}  "
                      f"{device['label']}")
            return 0
        if args.command == "revoke":
            if registry.revoke(args.id):
                print(f"revoked {args.id}")
                return 0
            print(f"no such active device: {args.id}", file=sys.stderr)
            return 1
    except (DeviceIdentityError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
