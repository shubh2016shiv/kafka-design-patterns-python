"""
CLI entrypoint for the content-based-router producer demo.

Why this module exists:
- provides the canonical package runner: `python -m producers.content_based_router`
- keeps demo execution discoverable at the package root like callback_confirmed
- avoids forcing users to remember the deeper `.demo` module path

ASCII flow:
    python -m producers.content_based_router
        |
        v
    Stage 1.0 configure concise, operator-friendly logging
        |
        v
    Stage 2.0 execute run_demo() from demo.py
        |
        v
    Stage 3.0 return exit code 0 when the demo completes
"""

from __future__ import annotations

import logging

from .demo import run_demo

logger = logging.getLogger(__name__)


def main() -> int:
    """
    Run the content-based router educational demo.

    Returns:
        Exit code `0` when demo execution completes.

    Failure behavior:
        Any unhandled exception propagates to Python and results in a non-zero
        process exit code, preserving traceback details for debugging.
    """
    # Stage 1.0 - Configure top-level logging for clean CLI output.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("=== Content-Based Router Teaching Demo ===")

    # Stage 2.0 - Execute the package demo.
    run_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
