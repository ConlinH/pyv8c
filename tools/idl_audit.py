"""CLI wrapper for the packaged declaration audit."""

import sys

from pyv8c.tools.idl_audit import main


if __name__ == "__main__":
    sys.exit(main())
