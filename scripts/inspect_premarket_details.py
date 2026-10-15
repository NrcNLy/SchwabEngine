"""
scripts/inspect_premarket_details.py
====================================
Detailed inspection script for pre-market readiness on schwab-trader VM.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.ses_mapping import DEFAULT_SNAPSHOT_PATH, load_ses_snapshot, ses_to_params


def inspect_all():
    print("--- [1] Checking SES Snapshot on Disk ---")
    snapshot_path = Path("data/exports/latest_ses_snapshot.json")
    if snapshot_path.exists():
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
        print(f"Timestamp: {raw.get('timestamp')}")
        print(f"SES Score: {raw.get('ses')}")
        print(f"Regime: {raw.get('regime')}")
        params = ses_to_params(raw.get("ses", 0.0))
        print(f"Derived Parameters: k_u={params.k_u:.4f}, k_l={params.k_l:.4f}, gamma={params.gamma:.4f}, regime={params.regime}")
    else:
        print("data/exports/latest_ses_snapshot.json does NOT exist!")

    print("\n--- [2] Checking Local SQLite Databases for Trades / Positions ---")
    for db_dir in [Path("state"), Path("/home/nicho/schwab_state")]:
        db_path = db_dir / "trades.db"
        if db_path.exists():
            print(f"Checking {db_path}...")
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cur.fetchall()]
            print(f"  Tables: {tables}")
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                count = cur.fetchone()[0]
                print(f"  Table {t}: {count} rows")
                if count > 0:
                    cur.execute(f"SELECT * FROM {t} LIMIT 5")
                    rows = cur.fetchall()
                    for r in rows:
                        print(f"    {r}")
            conn.close()

    print("\n--- [3] Checking API Endpoints ---")
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8080/api/status")
        status_data = json.loads(req.read().decode())
        print(f"Status: {status_data.get('status')}, Auth: {status_data.get('auth_status')}, Connected: {status_data.get('is_connected')}")
        print(f"NLV: ${status_data.get('nlv')}, Positions in status: {status_data.get('positions')}")
    except Exception as e:
        print(f"Error querying /api/status: {e}")

    try:
        req = urllib.request.urlopen("http://127.0.0.1:8080/api/positions/all?env=active")
        pos_data = json.loads(req.read().decode())
        print(f"Active Positions: managed={pos_data.get('managed')}, unmanaged={pos_data.get('unmanaged')}")
    except Exception as e:
        print(f"Error querying /api/positions/all: {e}")

    print("\n--- [4] Checking Strategy Config AVE Mode ---")
    cfg_path = Path("strategy_config.json")
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        print(f"AVE Config: {cfg.get('ave')}")


if __name__ == "__main__":
    inspect_all()
