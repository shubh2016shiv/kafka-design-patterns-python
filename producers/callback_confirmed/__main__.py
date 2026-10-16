"""
CLI entrypoint for callback-confirmed producer demo execution.

Why this module exists:
- provides a canonical executable target: `python -m producers.callback_confirmed`
- keeps demo execution colocated with callback_confirmed package implementation
- replaces legacy module-entrypoint dependency on `callback_confirmed_producer.py`

ASCII flow:
    python -m producers.callback_confirmed
        |
        v
    Stage 1.0 configure human-readable logging
        |
        v
    Stage 2.0 execute the reusable publish -> poll -> finish lifecycle
        |
        v
    Stage 3.0 print callback-aware delivery counters and exit honestly
"""

# Postpone type-hint evaluation so annotations cannot trigger runtime imports.
from __future__ import annotations

# logging provides readable tutorial output without placing print calls in the library.
import logging

# Import only the high-level demo orchestrator; workflow logic stays out of this entrypoint.
from .demo import run_callback_confirmed_demo

logger = logging.getLogger(__name__)


def main() -> int:
    """
    Run the callback-confirmed demo and emit an honest delivery summary.

    Returns:
        Exit code `0` when every accepted message was callback-confirmed;
        otherwise `1`, which allows shells and CI jobs to detect failure.

    Failure behavior:
        Any unhandled exception bubbles to the Python runtime, resulting in a
        non-zero process exit code and traceback suitable for debugging.
    """
    # Show informational teaching messages with their logger name and severity.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Mark the start so terminal output from Kafka dependencies has clear context.
    logger.info("=== Callback-Confirmed Producer Teaching Demo ===")

    # Execute create-once, publish, poll, and finish in the demo module.
    summary = run_callback_confirmed_demo()
    # Print every state because flush output alone cannot prove callback success.
    logger.info("Queued locally: %d", summary.queued)
    logger.info("Callback-confirmed delivery: %d", summary.delivered)
    logger.info("Final delivery failures: %d", summary.failed)
    logger.info("Still awaiting callback: %d", summary.awaiting_callback)
    logger.info("Producer work remaining: %d", summary.producer_remaining)
    logger.info("All accepted events confirmed: %s", summary.all_confirmed)
    # Use a conventional zero/nonzero exit status for automation.
    return 0 if summary.all_confirmed else 1


# Python sets this name only when executing the module, not when importing it.
if __name__ == "__main__":
    # Convert main's integer result into the operating-system process exit code.
    raise SystemExit(main())
