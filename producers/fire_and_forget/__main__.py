"""
CLI entrypoint for the fire-and-forget producer demo.

Why this module exists:
- provides the canonical package runner: `python -m producers.fire_and_forget`
- mirrors callback_confirmed package-level execution ergonomics
- keeps demo discovery simple without requiring the `.demo` suffix

ASCII flow:
    python -m producers.fire_and_forget
        |
        v
    Stage 1.0 configure concise educational logging
        |
        v
    Stage 2.0 execute run_demo() from demo.py
        |
        v
    Stage 3.0 return exit code 0 on completion
"""

from __future__ import annotations

import logging

from .demo import run_demo

logger = logging.getLogger(__name__)


def main() -> int:
    """
    Run the fire-and-forget educational demo scenes.

    Returns:
        Exit code `0` when the demo completes successfully.

    Failure behavior:
        Unhandled exceptions propagate and yield non-zero process exit status,
        preserving traceback details for troubleshooting.
    """
    # Stage 1.0 - Configure readable top-level logging for CLI use.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("=== Fire-and-Forget Producer Teaching Demo ===")

    # Stage 2.0 - Execute the package demo.
    run_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
