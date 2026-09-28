"""revgate command line."""

from __future__ import annotations

import argparse
from pathlib import Path

from revgate import __version__
from revgate.config import ConfigError, parse_age
from revgate.gate_cache import gate_stats, run_gate_if_changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="revgate")
    parser.add_argument("--version", action="version", version=f"revgate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    gic = sub.add_parser("gate-if-changed", help="skip a command that passed on this exact tree")
    gic.add_argument("--max-age", default=None, help="for example 24h (default: [gates.cache])")
    gic.add_argument("--no-cache", action="store_true", help="run even on a cache hit")
    gic.add_argument("cmd", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    stats = sub.add_parser("gate-stats", help="print the gate cache's hit rate")
    stats.add_argument("--since", default=None, help="for example 7d")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "gate-if-changed":
            cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
            max_age = parse_age(args.max_age) if args.max_age else None
            return run_gate_if_changed(
                cmd, cwd=Path.cwd(), max_age_s=max_age, no_cache=args.no_cache
            )
        if args.command == "gate-stats":
            since = parse_age(args.since) if args.since else None
            return gate_stats(Path.cwd(), since)
    except ConfigError as exc:
        print(f"revgate: {exc}")
        return 2
    return 2
