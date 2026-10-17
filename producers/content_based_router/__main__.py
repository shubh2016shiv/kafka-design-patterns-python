"""
Command-line entry point for the content-based router demo.

Run it with:
    python -m producers.content_based_router

By default this runs the OFFLINE demo (no Kafka needed). Set the environment
variable RUN_LIVE_KAFKA=true to publish to a real broker instead.
"""

from __future__ import annotations

# os: read the opt-in flag that chooses offline vs live.
import os

# logging: show the demo's labeled output on the console.
import logging

from .demo import run_live_demo, run_offline_demo

logger = logging.getLogger("producers.content_based_router")


# ============================================================================
# STEP 1: main — pick a mode, run it, report the counters honestly
# ============================================================================


def main() -> int:
    """Run the demo and return a process exit code (0 = all delivered).

    Returns 1 when any message failed delivery or is still unconfirmed, so a
    shell or CI job can detect a problem from the exit code alone.
    """
    # Show INFO lines with their logger name, so demo output is easy to follow.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # "true"/"1"/"yes" (any case) turns on the live path; anything else stays offline.
    run_live = os.environ.get("RUN_LIVE_KAFKA", "").strip().lower() in {"true", "1", "yes"}

    logger.info("=== Content-Based Router demo (%s) ===", "LIVE" if run_live else "OFFLINE")
    snapshot = run_live_demo() if run_live else run_offline_demo()

    # Print each counter: flush finishing is not proof that everything succeeded.
    logger.info("accepted locally:   %d", snapshot["accepted"])
    logger.info("delivered (ack'd):  %d", snapshot["delivered"])
    logger.info("delivery failures:  %d", snapshot["failed"])
    logger.info("awaiting callbacks: %d", snapshot["awaiting_callbacks"])

    all_confirmed = snapshot["failed"] == 0 and snapshot["awaiting_callbacks"] == 0
    return 0 if all_confirmed else 1


# Python sets __name__ to "__main__" only when this module is executed directly.
if __name__ == "__main__":
    # Turn the integer result into the operating-system exit status.
    raise SystemExit(main())
