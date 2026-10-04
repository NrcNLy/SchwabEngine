"""One-off: point modules at core.paths (scratch)."""
import re
from pathlib import Path

root = Path(r"c:\Projects\schwab_engine")

def edit(rel, pairs):
    p = root / rel
    s = p.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in s:
            raise SystemExit(f"{rel}: missing {old!r}")
        s = s.replace(old, new)
    p.write_text(s, encoding="utf-8", newline="")
    print("ok", rel)

edit("core/liquidity_policy.py", [('POLICY_PATH = Path("data/liquidity_policy.json")', 'POLICY_PATH = POLICY_FILE')])
edit("core/runtime.py", [
    ('NlvAnchor("data/nlv_anchor_active.json")', 'NlvAnchor(ANCHOR_ACTIVE_FILE)'),
    ('NlvAnchor("data/nlv_anchor_sandbox.json")', 'NlvAnchor(ANCHOR_SANDBOX_FILE)'),
    ('read_json("data/macro_liquidity.json", default={})', 'read_json(MACRO_STATE_FILE, default={})'),
    ('from core.atomic_io import read_json\n', 'from core.atomic_io import read_json\nfrom core.paths import ANCHOR_ACTIVE_FILE, ANCHOR_SANDBOX_FILE, MACRO_STATE_FILE\n'),
])
edit("services/document_parser.py", [
    ('self.output_dir = output_dir or Path("data")', 'self.output_dir = output_dir or STATE_DIR'),
    ('self.hashes_file = Path("data/vault/documents/processed_hashes.json")', 'self.hashes_file = PROCESSED_HASHES_FILE'),
    ('data/macro_liquidity.json (temp', 'state/macro_liquidity.json (temp'),
    ('from core.atomic_io import atomic_write_json, read_json, update_json\n', 'from core.atomic_io import atomic_write_json, read_json, update_json\nfrom core.paths import PROCESSED_HASHES_FILE, STATE_DIR\n'),
])
edit("api/server.py", [
    ('MACRO_STATE_FILE = Path("data/macro_liquidity.json")\n', ''),
    ('DOCUMENTS_VAULT_DIR = Path("data/vault/documents")\n', 'DOCUMENTS_VAULT_DIR = DOCUMENTS_DIR\n'),
    ('from core.liquidity_policy import', 'from core.paths import DOCUMENTS_DIR, MACRO_STATE_FILE\nfrom core.liquidity_policy import'),
])
edit("main.py", [
    ('state_file = Path("data/macro_liquidity.json")', 'state_file = MACRO_STATE_FILE'),
    ('Watches data/macro_liquidity.json', 'Watches state/macro_liquidity.json'),
    ('from core.models import MarketEvent\n', 'from core.paths import MACRO_STATE_FILE\n'),
])
