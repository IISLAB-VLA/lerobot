"""CLI entry point for `lerobot-dashboard`.

The real implementation (FastAPI app + uvicorn runner) lands with task #2.
This placeholder keeps the `[project.scripts]` wiring importable so that
`uv sync --extra dashboard` installs a valid console script.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "lerobot-dashboard is not yet implemented. "
        "The FastAPI app + uvicorn runner arrive with task #2."
    )


if __name__ == "__main__":
    main()
