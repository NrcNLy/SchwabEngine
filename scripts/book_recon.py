"""
scripts/book_recon.py
=====================
Read-only Schwab streamer reconnaissance for the microstructure engine.

Places NO orders and touches no ledger/state besides writing its own report. It logs in
to the streamer, subscribes the candidate services, listens for ``--seconds`` and reports,
per (service, symbol): the SUBS acknowledgement code/message, frames received, observed
depth levels (books), update rate and a raw sample of the first frame.

Run on the VM (vault + network required):
    python scripts/book_recon.py --seconds 60

Report is printed and written to ``$ENGINE_STATE_DIR/book_recon.json`` (default ``state/``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EQUITY_FIELDS = "0,1,2,3,4,5,8,9,10,11,12"
FUTURES_FIELDS = "0,1,2,3,4,5,8,9"
BOOK_FIELDS = "0,1,2,3"


def _depth_of(content: Dict[str, Any]) -> Dict[str, int]:
    """Counts price levels on each side of a Schwab BOOK content item (fields 2 = bids, 3 = asks)."""
    out = {"bid_levels": 0, "ask_levels": 0}
    for key, side in (("2", "bid_levels"), ("3", "ask_levels")):
        levels = content.get(key)
        if isinstance(levels, list):
            out[side] = len(levels)
    return out


class Recon:
    def __init__(self) -> None:
        self.acks: Dict[str, Dict[str, Any]] = {}
        self.stats: Dict[str, Dict[str, Any]] = {}
        self.requests: Dict[str, str] = {}

    def record_ack(self, request_id: str, code: Any, msg: str) -> None:
        label = self.requests.get(request_id, request_id)
        self.acks[label] = {"code": code, "msg": msg}

    def record_data(self, service: str, content: Dict[str, Any]) -> None:
        symbol = str(content.get("key", content.get("0", "?")))
        slot = self.stats.setdefault(f"{service}|{symbol}", {
            "service": service, "symbol": symbol, "frames": 0, "first_ts": None, "last_ts": None,
            "max_bid_levels": 0, "max_ask_levels": 0, "sample": None, "field_keys": [],
        })
        now = time.monotonic()
        slot["frames"] += 1
        slot["first_ts"] = slot["first_ts"] or now
        slot["last_ts"] = now
        if slot["sample"] is None:
            slot["sample"] = content
        keys = set(slot["field_keys"]) | {str(k) for k in content.keys()}
        slot["field_keys"] = sorted(keys, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else 0, k))
        if "BOOK" in service:
            depth = _depth_of(content)
            slot["max_bid_levels"] = max(slot["max_bid_levels"], depth["bid_levels"])
            slot["max_ask_levels"] = max(slot["max_ask_levels"], depth["ask_levels"])

    def report(self) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for slot in self.stats.values():
            span = (slot["last_ts"] - slot["first_ts"]) if slot["frames"] > 1 else 0.0
            rows.append({
                "service": slot["service"], "symbol": slot["symbol"], "frames": slot["frames"],
                "rate_hz": round(slot["frames"] / span, 2) if span > 0 else None,
                "bid_levels": slot["max_bid_levels"], "ask_levels": slot["max_ask_levels"],
                "field_keys": slot["field_keys"], "sample": slot["sample"],
            })
        return {"acks": self.acks, "streams": sorted(rows, key=lambda r: (r["service"], r["symbol"]))}


def _req(info: Dict[str, Any], recon: Recon, label: str, service: str, keys: List[str], fields: str) -> Dict[str, Any]:
    from data.streamer import _next_request_id

    rid = str(_next_request_id())
    recon.requests[rid] = label
    return {
        "service": service, "requestid": rid, "command": "SUBS",
        "SchwabClientCustomerId": info["schwabClientCustomerId"],
        "SchwabClientCorrelId": info["schwabClientCorrelId"],
        "parameters": {"keys": ",".join(keys), "fields": fields},
    }


async def run(seconds: float, equities: List[str], books: Dict[str, List[str]], futures: List[str]) -> Dict[str, Any]:
    import websockets
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from core.auth import SchwabAuthManager, SecurityVault
    from core.runtime import load_config
    from data.rest_client import SchwabRestClient
    from data.streamer import SchwabStreamer

    cfg = load_config()
    auth_cfg = cfg.get("auth", {}) or {}
    vault = SecurityVault(
        passphrase=os.environ["VAULT_PASSPHRASE"],
        iterations=int(auth_cfg.get("pbkdf2_iterations", 600000)),
        vault_path=ROOT / auth_cfg.get("vault_file", "schwab_tokens_vault.json"),
    )
    auth = SchwabAuthManager(os.environ["SCHWAB_CLIENT_ID"], os.environ["SCHWAB_CLIENT_SECRET"], vault, cfg)
    auth.load_tokens()
    rest = SchwabRestClient.from_config(cfg, auth)
    streamer = SchwabStreamer.from_config(cfg, rest)

    recon = Recon()
    info = streamer._fetch_streamer_credentials()
    ws_url = str(info.get("streamerSocketUrl") or streamer._ws_url)
    print(f"Connecting to {ws_url}")
    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
        await streamer._send_admin_login(ws, info)
        if not await streamer._recv_and_validate(ws, "LOGIN"):
            raise RuntimeError("streamer LOGIN rejected")

        requests: List[Dict[str, Any]] = []
        if equities:
            requests.append(_req(info, recon, "LEVELONE_EQUITIES:" + ",".join(equities),
                                 "LEVELONE_EQUITIES", equities, EQUITY_FIELDS))
        for service, syms in books.items():
            requests.append(_req(info, recon, f"{service}:" + ",".join(syms), service, syms, BOOK_FIELDS))
        if futures:
            requests.append(_req(info, recon, "LEVELONE_FUTURES:" + ",".join(futures),
                                 "LEVELONE_FUTURES", futures, FUTURES_FIELDS))
        for r in requests:
            await ws.send(json.dumps({"requests": [r]}))

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for resp in msg.get("response", []) or []:
                content = resp.get("content", {}) or {}
                recon.record_ack(str(resp.get("requestid", "")), content.get("code"), str(content.get("msg", "")))
            for frame in msg.get("data", []) or []:
                service = str(frame.get("service", ""))
                for content in frame.get("content", []) or []:
                    recon.record_data(service, content)
    return recon.report()


def print_report(rep: Dict[str, Any]) -> None:
    print("\n=== SUBS acknowledgements ===")
    for label, ack in rep["acks"].items():
        status = "OK  " if ack["code"] == 0 else "FAIL"
        print(f"[{status}] {label:<60} code={ack['code']} msg={ack['msg']}")
    print("\n=== Streams observed ===")
    if not rep["streams"]:
        print("(no data frames received - market may be closed or the services are not entitled)")
    for row in rep["streams"]:
        depth = f" bid_lv={row['bid_levels']} ask_lv={row['ask_levels']}" if "BOOK" in row["service"] else ""
        print(f"{row['service']:<18} {row['symbol']:<8} frames={row['frames']:<5} rate_hz={row['rate_hz']}{depth} fields={row['field_keys']}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Schwab streamer book/feed recon")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--equities", default="TQQQ,SOXL,TNA,NVDA,TSM,KRE,$TNX")
    ap.add_argument("--nasdaq-book", default="TQQQ,SOXL,TNA,NVDA,TSM")
    ap.add_argument("--nyse-book", default="TQQQ,SOXL,TNA,KRE")
    ap.add_argument("--futures", default="/ZN,/NQ")
    args = ap.parse_args(argv)

    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
    books = {"NASDAQ_BOOK": split(args.nasdaq_book), "NYSE_BOOK": split(args.nyse_book)}
    books = {k: v for k, v in books.items() if v}
    rep = asyncio.run(run(args.seconds, split(args.equities), books, split(args.futures)))
    print_report(rep)

    out_dir = Path(os.environ.get("ENGINE_STATE_DIR", "state"))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "book_recon.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(f"\nReport written to {out_dir / 'book_recon.json'}")
    except OSError as exc:
        print(f"\nCould not write report: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
