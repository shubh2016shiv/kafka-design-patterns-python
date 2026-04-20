"""
CLI entrypoint for the dead-letter-queue producer demo.

Why this module exists:
- provides the canonical package runner: `python -m producers.dead_letter_queue`
- mirrors callback_confirmed package ergonomics for consistency
- keeps DLQ learning/demo workflows easy to discover from the package root

ASCII flow:
    python -m producers.dead_letter_queue
        |
        v
    Stage 1.0 configure readable CLI logging
        |
        v
    Stage 2.0 execute run_demo() from demo.py
        |
        v
    Stage 3.0 return exit code 0 when the walkthrough completes
"""

from __future__ import annotations

import logging

from .demo import run_demo

logger = logging.getLogger(__name__)


def main() -> int:
    """
    Run the dead-letter-queue educational demo workflow.

    Returns:
        Exit code `0` when the demo finishes.

    Failure behavior:
        Any unhandled exception bubbles up and the process exits non-zero with
        a traceback for operational debugging.
    """
    # Stage 1.0 - Keep top-level CLI output compact and readable.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("=== Dead Letter Queue Producer Teaching Demo ===")

    # Stage 2.0 - Execute demo stages defined in demo.py.
    run_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
