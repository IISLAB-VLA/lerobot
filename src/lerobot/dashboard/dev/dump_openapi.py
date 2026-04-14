"""Dump the dashboard's OpenAPI schema to stdout or a file.

Used by the frontend ``generate:api-types`` script (task #8) to produce
``openapi-typescript`` definitions without needing a running server.

Usage::

    uv run python -m lerobot.dashboard.dev.dump_openapi            # stdout
    uv run python -m lerobot.dashboard.dev.dump_openapi out.json   # file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lerobot.dashboard.app import create_app


def dump(output: Path | None) -> None:
    schema = create_app().openapi()
    payload = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(payload)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lerobot.dashboard.dev.dump_openapi",
        description="Write the FastAPI OpenAPI schema as JSON.",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=None,
        help="Destination file (default: stdout).",
    )
    args = parser.parse_args(argv)
    dump(args.output)


if __name__ == "__main__":
    main()
