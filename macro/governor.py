"""
macro/governor.py
=================
Macro Strategy Governor entrypoint.
Executes the Tier 2 Background Strategy Governor daemon.
"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from governor import GovernorConnectionError, GovernorDaemon, main

if __name__ == "__main__":
    sys.exit(main())
