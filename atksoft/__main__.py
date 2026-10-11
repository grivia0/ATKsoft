"""``python -m atksoft`` -- the same entry point as the ``atksoft`` script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
