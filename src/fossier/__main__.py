"""Allow running as `python -m fossier`."""

import sys

from fossier.cli import main

sys.exit(main())
