"""
server.py
=========
Convenience entrypoint kept for backwards compatibility. The API now runs inside the same
process as the engine (see main.py), so this simply starts main.py's DRY-RUN mode with the API.

    python server.py            == python main.py
    python server.py --test     == python main.py --test
"""

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from main import parse_args, run, run_self_test  # noqa: E402


if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    args = parse_args()
    if args.test:
        sys.exit(run_self_test())
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass
