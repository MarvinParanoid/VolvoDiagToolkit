"""Run the CLI without installing: `PYTHONPATH=python python -m volvo_diag ...`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
