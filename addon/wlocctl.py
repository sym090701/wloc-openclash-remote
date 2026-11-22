#!/usr/bin/env python3
"""Offline maintenance helper for WLOC Control state."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from wloc_config import ConfigError, WlocConfigStore, enabled_device_cidrs
from wloc_web import AuthStore


def _password_from_input(use_stdin: bool) -> str:
    if use_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New WLOC WebUI password: ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            raise ConfigError("passwords do not match")
    return password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wlocctl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("auth-init", "auth-reset"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--path", default="/opt/wloc-state/auth.json")
        subparser.add_argument("--password-stdin", action="store_true")

    show = subparsers.add_parser("show")
    show.add_argument("--config", default="/opt/wloc-state/wloc.json")
    show.add_argument("--provider", default="/opt/wloc-rules/wloc-clients.yaml")
    show.add_argument("--backups", default="/opt/wloc-state/backups")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command in ("auth-init", "auth-reset"):
            password = _password_from_input(args.password_stdin)
            AuthStore.bootstrap(
                args.path,
                password,
                replace=args.command == "auth-reset",
            )
            print(f"authentication updated: {args.path}")
            return 0

        if args.command == "show":
            store = WlocConfigStore(args.config, args.provider, args.backups)
            config = store.load()
            output = {
                "config": config,
                "enabled_device_cidrs": enabled_device_cidrs(config),
                "backup_count": len(store.list_backups()),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
    except (ConfigError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
