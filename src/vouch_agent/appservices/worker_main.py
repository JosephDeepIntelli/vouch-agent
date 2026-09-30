"""Detached worker entry: python -m vouch_agent.appservices.worker_main."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vouch-worker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--provider", default="extract-fact")
    parser.add_argument(
        "--command",
        choices=("start", "resume"),
        default="start",
        help="explicit worker command; once started, PAUSED is a stopping "
        "condition requiring a separate resume command",
    )
    parser.add_argument(
        "--command-version",
        type=int,
        default=None,
        help="expected durable command version; a mismatch refuses (stale/duplicate dispatch)",
    )
    args = parser.parse_args(argv)
    from vouch_agent.appservices.worker_lifecycle import execute_run_detached

    return execute_run_detached(
        Path(args.project),
        args.run,
        provider=args.provider,
        command=args.command,
        command_version=args.command_version,
    )


if __name__ == "__main__":
    sys.exit(main())
