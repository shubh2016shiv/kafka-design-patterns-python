"""Lets you run the demo with: python -m producers.callback_confirmed

The demo itself lives in main.py; this file only forwards to it.
"""

# Python runs this file when you type "python -m producers.callback_confirmed".
# We import the real demo function from main.py (the dot means "this same package").
from .main import main

# This guard means "only run when started from the command line", so merely importing
# this file (for example in a test) does not start the demo.
if __name__ == "__main__":
    # SystemExit(n) ends the program with exit code n. main() returns 0 for "all
    # confirmed" and 1 otherwise, so scripts and CI can detect a failed run.
    raise SystemExit(main())
