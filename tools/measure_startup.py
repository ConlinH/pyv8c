"""CLI wrapper for the packaged startup measurement."""

import sys

from pyv8c.tools.measure_startup import main


if __name__ == "__main__":
    sys.exit(main())
