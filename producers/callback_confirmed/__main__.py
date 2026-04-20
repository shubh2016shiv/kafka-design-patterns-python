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
    Stage 2.0 execute run_demo_round_trip()
        |
        v
    Stage 3.0 log produce + consume verification outcome
"""

from __future__ import annotations

import json
import logging

from .demo import run_demo_round_trip

logger = logging.getLogger(__name__)


def main() -> int:
    """
    Run the callback-confirmed demo and emit structured educational logs.

    Returns:
        Exit code `0` when demo execution completes successfully.

    Failure behavior:
        Any unhandled exception bubbles to the Python runtime, resulting in a
        non-zero process exit code and traceback suitable for debugging.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("=== Callback-Confirmed Producer Teaching Demo ===")

    round_trip_result = run_demo_round_trip()
    logger.info("Produce result: %s", round_trip_result.produce_result)
    logger.info("Consume-back verification: %s", round_trip_result.consume_verification)
    if round_trip_result.consume_verification.received_event is not None:
        logger.info(
            "Event received back from topic:\n%s",
            json.dumps(
                round_trip_result.consume_verification.received_event, indent=2, sort_keys=True
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
