#!/usr/bin/env python3
"""python simulator/backfill.py --days 30 --step 15m  (ver fpsim/backfill.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fpsim.backfill import main  # noqa: E402

if __name__ == "__main__":
    main()
