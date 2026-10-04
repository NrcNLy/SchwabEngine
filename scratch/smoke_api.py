"""Quick API smoke test (scratch)."""
import json
from fastapi.testclient import TestClient

from api.server import build_app
from core.runtime import EngineContext, build_ledger, load_config
from execution.risk_manager import RiskEngine

cfg = load_config()
ctx = EngineContext(cfg, live=False)
ctx.ledgers["sandbox"] = build_ledger(cfg, live=False)
ctx.risk_manager = RiskEngine()
app = build_app(ctx)
c = TestClient(app)

for path in ["/status", "/api/status", "/status?env=active", "/ledger", "/api/ledger?env=active", "/positions/all",
             "/orders", "/v1/liquidity/policy", "/api/v1/liquidity/buying-power", "/api/v1/regime/summary",
             "/api/v1/liquidity/state", "/health"]:
    r = c.get(path)
    print(path, r.status_code, json.dumps(r.json())[:260])

r = c.put("/api/v1/liquidity/policy", json={"sweep": {"mode": "AUTO_WITH_APPROVAL"}})
print("policy auto", r.status_code)
r = c.get("/status?env=bogus")
print("bad env", r.status_code)
r = c.post("/emergency/liquidate")
print("liquidate", r.json())
with c.websocket_connect("/stream") as ws:
    print("ws", ws.receive_json())
