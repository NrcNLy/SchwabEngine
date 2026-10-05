"""HTTP contract tests for the FastAPI layer (TestClient; no network, no broker)."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.server import build_app
from core.liquidity_policy import POLICY_PATH
from core.runtime import EngineContext, build_ledger, load_config
from execution.risk_manager import RiskEngine


@pytest.fixture()
def ctx():
    cfg = load_config()
    c = EngineContext(cfg, live=False)
    c.ledgers["sandbox"] = build_ledger(cfg, live=False)
    c.risk_manager = RiskEngine()
    return c


@pytest.fixture()
def client(ctx):
    return TestClient(build_app(ctx))


STATUS_KEYS = {
    "status", "system_state", "system_reasons", "auth_status", "auth_expires_in_s", "env", "data_source",
    "engine_mode", "is_connected", "uptime_seconds", "active_positions", "nlv", "net_change_usd",
    "net_change_pct", "today_pnl", "today_realized_pnl", "unrealized_pnl", "circuit_breaker_limit",
    "circuit_breaker_triggered", "external_positions_detected", "timestamp_edt", "lifecycle_phase",
    "session_phase", "entry_permitted", "phase_sizing_multiplier", "vm_stats", "microstructure",
}


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_status_contract_and_dual_mounting(client, prefix):
    r = client.get(f"{prefix}/status?env=sandbox")
    assert r.status_code == 200
    body = r.json()
    assert STATUS_KEYS <= set(body)
    assert body["env"] == "sandbox" and body["data_source"] == "SANDBOX_SIM"
    assert body["system_state"] == "SIMULATED" and body["auth_status"] == "NOT_REQUIRED"
    assert body["nlv"] == pytest.approx(1000.0)


def test_default_env_follows_engine_mode(client):
    assert client.get("/api/status").json()["env"] == "sandbox"


def test_unknown_env_is_rejected(client):
    for path in ("/api/status", "/api/ledger", "/api/positions/all", "/api/orders"):
        assert client.get(f"{path}?env=bogus").status_code == 422


def test_active_env_is_unavailable_in_dry_run_not_faked(client):
    status = client.get("/api/status?env=active").json()
    assert status["data_source"] == "UNAVAILABLE" and status["nlv"] is None
    ledger = client.get("/api/ledger?env=active").json()
    assert ledger["available"] is False and ledger["buying_power"] is None
    assert ledger["total_nlv"] == 0 and ledger["max_single_exposure"] == 0
    positions = client.get("/api/positions/all?env=active").json()
    assert positions["data_source"] == "UNAVAILABLE" and positions["managed"] == [] and positions["unmanaged"] == []


def test_ledger_cap_is_exactly_20pct_of_nlv(client, ctx):
    body = client.get("/api/ledger?env=sandbox").json()
    assert body["available"] is True
    assert body["max_single_exposure"] == pytest.approx(body["total_nlv"] * 0.20)
    bp = body["buying_power"]
    assert bp["single_ticker_cap"] == pytest.approx(body["total_nlv"] * 0.20)
    assert bp["max_order_notional"] <= body["max_single_exposure"]
    assert bp["sweep"]["routed"] is False and bp["sweep"]["mode"] == "ADVISORY"

    # grow NLV: the cap follows, nothing is hardcoded
    ctx.ledgers["sandbox"].set_pending_ach(0)
    ctx.ledgers["sandbox"]._settled += 2747  # type: ignore[attr-defined]
    body = client.get("/api/ledger?env=sandbox").json()
    assert body["total_nlv"] == pytest.approx(3747.0)
    assert body["max_single_exposure"] == pytest.approx(749.4)


def test_policy_defaults_roundtrip_and_validation(client, ctx):
    pol = client.get("/api/v1/liquidity/policy").json()
    assert pol["inflow"]["amount"] == 250 and pol["inflow"]["weekday"] == "FRIDAY"
    assert pol["inflow"]["lag_business_days"] == 3 and pol["inflow"]["confidence"] == pytest.approx(0.8)
    assert pol["sweep"]["mode"] == "ADVISORY"

    pol["inflow"]["amount"] = 400.0
    pol["inflow"]["weekday"] = "THURSDAY"
    saved = client.put("/api/v1/liquidity/policy", json=pol)
    assert saved.status_code == 200
    assert POLICY_PATH.exists()
    again = client.get("/api/v1/liquidity/policy").json()
    assert again["inflow"]["amount"] == 400 and again["inflow"]["weekday"] == "THURSDAY"
    assert ctx.get_policy().inflow.weekday == "THURSDAY"

    bad_mode = {**again, "sweep": {**again["sweep"], "mode": "AUTO_WITH_APPROVAL"}}
    assert client.put("/api/v1/liquidity/policy", json=bad_mode).status_code == 422
    bad_day = {**again, "inflow": {**again["inflow"], "weekday": "SATURDAY"}}
    assert client.put("/api/v1/liquidity/policy", json=bad_day).status_code == 422
    assert client.get("/api/v1/liquidity/policy").json()["inflow"]["weekday"] == "THURSDAY"


def test_regime_summary_is_plain_language(client):
    body = client.get("/api/v1/regime/summary").json()
    for key in ("headline", "bias_label", "regime_label", "volatility_label", "stale", "advisory_note"):
        assert key in body
    assert "prompt" not in body and "last_response" not in body


def test_liquidity_state_contains_no_fabricated_sample_data(client):
    body = client.get("/api/v1/liquidity/state").json()
    assert body["success"] is True and body["promotional_debts"] == [] and body["snapshot"] is None
    assert body["file_source"] is None and body["_is_simulated"] is True
    assert body["state"]["settled_cash"] == pytest.approx(1000.0)


def test_halt_and_resume_are_reflected_in_system_state(client):
    assert client.post("/api/emergency/halt", json={"halted": True}).json()["halted"] is True
    assert client.get("/api/status?env=sandbox").json()["system_state"] == "HALTED"
    assert client.post("/api/emergency/halt", json={"halted": False}).json()["halted"] is False
    assert client.get("/api/status?env=sandbox").json()["system_state"] == "SIMULATED"


def test_liquidate_reports_the_truth(client, ctx):
    r = client.post("/api/emergency/liquidate").json()
    assert r["success"] is False and r["liquidated_count"] == 0  # nothing registered to flatten

    async def flatten(reason: str) -> int:
        return 2

    ctx.flatten_cb = flatten
    ok = client.post("/api/emergency/liquidate").json()
    assert ok["success"] is True and ok["liquidated_count"] == 2


def test_controls_never_fake_success_when_engine_parts_are_missing(client):
    assert client.post("/api/strategy/regime_a/toggle").status_code == 503
    assert client.post("/api/strategy/config", json={}).status_code in (422, 503)
    assert client.post("/api/auth/refresh").json()["success"] is False
    assert client.post("/api/auth/exchange", json={"code": "x"}).status_code == 503


def test_upload_validation(client):
    assert client.post("/api/v1/documents/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415
    assert client.post("/api/v1/documents/upload", files={"file": ("empty.pdf", b"", "application/pdf")}).status_code == 400
    big = b"0" * (20 * 1024 * 1024 + 1)
    assert client.post("/api/v1/documents/upload", files={"file": ("big.pdf", big, "application/pdf")}).status_code == 413


def test_upload_failure_is_reported_not_fabricated(client, monkeypatch):
    from services import document_parser

    async def boom(self, path):
        raise document_parser.DocumentExtractionError("model unavailable")

    monkeypatch.setattr(document_parser.DocumentParser, "extract_document", boom)
    r = client.post("/api/v1/documents/upload", files={"file": ("stmt.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert r.status_code == 200 and r.json()["status"] == "QUEUED"
    job = client.get(f"/api/v1/documents/status/{r.json()['saved_as']}").json()
    assert job["status"] == "FAILED" and "model unavailable" in job["error"]
    assert client.get("/api/v1/liquidity/state").json()["snapshot"] is None


def test_upload_success_and_zero_auto_throttling(client, ctx, monkeypatch):
    from services import document_parser

    async def ok(self, path):
        return SimpleNamespace(document_class=SimpleNamespace(value="PAYSTUB")), False

    monkeypatch.setattr(document_parser.DocumentParser, "extract_document", ok)
    before = ctx.risk_manager.macro_risk_multiplier
    r = client.post("/api/v1/documents/upload", files={"file": ("pay.png", b"\x89PNG test", "image/png")})
    job = client.get(f"/api/v1/documents/status/{r.json()['saved_as']}").json()
    assert job["status"] == "DONE" and job["document_class"] == "PAYSTUB"
    assert ctx.risk_manager.macro_risk_multiplier == before == 1.0
    assert client.get("/api/v1/documents/status/unknown.pdf").status_code == 404


def test_websocket_heartbeat(client):
    with client.websocket_connect("/api/stream") as ws:
        msg = ws.receive_json()
    assert msg["event"] == "HEARTBEAT" and msg["system_state"] == "SIMULATED"


def test_indicators_endpoint(client):
    r = client.get("/api/indicators")
    assert r.status_code == 200
    body = r.json()
    assert "enabled" in body


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_live_monitoring_endpoint(client, prefix):
    r = client.get(f"{prefix}/live")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    text = r.text
    assert "SchwabEngine" in text
    assert "cdn.tailwindcss.com" in text
    assert "Engine Mode" in text
    assert "T+1 Rule Adherence" in text
    assert "Current Trading Phase" in text
    assert "AI Engine Confidence" in text
    assert "Capital Summary" in text
    assert "Holdings &amp; Capacity Ledger" in text or "Holdings & Capacity Ledger" in text
    assert "TQQQ" in text and "SOXL" in text and "TNA" in text
    assert "Reverse-Chronological Live Event Log" in text


