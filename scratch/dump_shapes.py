"""Dump full JSON shapes of API responses (scratch)."""
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

for path in ["/api/status?env=sandbox", "/api/status?env=active", "/api/ledger?env=sandbox", "/api/ledger?env=active",
             "/api/positions/all?env=sandbox", "/api/orders?env=sandbox",
             "/api/v1/liquidity/policy", "/api/v1/liquidity/buying-power?env=sandbox",
             "/api/v1/regime/summary", "/api/v1/liquidity/state"]:
    r = c.get(path)
    print("###", path, r.status_code)
    print(json.dumps(r.json(), indent=1))
