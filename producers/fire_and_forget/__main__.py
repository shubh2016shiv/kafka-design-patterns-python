"""
CLI entrypoint: `python -m producers.fire_and_forget`

WHY THIS MODULE EXISTS
    It makes the package runnable without having to know the `.demo` suffix.
    Python runs `__main__.py` when you execute a package with `-m`.

WHY IT IS THIS SHORT
    An entrypoint should only wire things together, never hold logic. All of
    the teaching content lives in `demo.py`, and the pattern itself lives in
    `core.py`. Logging is configured by `run_demo()` rather than here, so the
    tutorial's own output is not drowned out by library log lines.

    python -m producers.fire_and_forget
        |
        v
    Stage 1.0  run_demo() from demo.py
        |
        v
    Stage 2.0  exit code 0 on completion
"""

# Postpone evaluation of type hints. This keeps annotations lightweight and
# allows forward references without quoting every type name.
from __future__ import annotations

# Import only the tutorial orchestrator; an entrypoint should not contain the
# Kafka workflow itself.
from .demo import run_demo


def main() -> int:
    """Run the fire-and-forget tutorial.

    Returns:
        0 when the tutorial completes.

    Failure behaviour:
        Exceptions propagate, so the traceback is preserved and the process
        exits non-zero. That is deliberate: a broken demo should fail
        visibly rather than report success.
    """
    # Stage 1.0 - run the tutorial (it configures its own logging).
    run_demo()

    # Stage 2.0 - signal success to the shell.
    return 0


# Python sets ``__name__`` to ``"__main__"`` only when this file is executed,
# not when another module imports it for testing.
if __name__ == "__main__":
    # Convert main()'s integer into the process exit code seen by the shell.
    raise SystemExit(main())
