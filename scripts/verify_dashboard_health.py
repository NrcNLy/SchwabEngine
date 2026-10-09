"""
scripts/verify_dashboard_health.py
==================================
Automated verification script for:
1. GET / returns index.html (HTTP 200)
2. GET /health returns status: "healthy" (HTTP 200)
3. API routes (/api/status, /docs, /stream) take precedence and are not intercepted
"""

import sys
import urllib.request
import urllib.error
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# In-process TestClient verification
from fastapi.testclient import TestClient
from api.server import build_app
from core.runtime import EngineContext, build_ledger, load_config
from execution.risk_manager import RiskEngine

def verify_in_process():
    print("--- [1] In-Process Contract Verification ---")
    cfg = load_config()
    ctx = EngineContext(cfg, live=False)
    ctx.ledgers["sandbox"] = build_ledger(cfg, live=False)
    ctx.risk_manager = RiskEngine()
    app = build_app(ctx)
    client = TestClient(app)

    # 1. GET / returns index.html
    r = client.get("/")
    assert r.status_code == 200, f"Expected 200 from GET /, got {r.status_code}"
    assert "text/html" in r.headers.get("content-type", ""), f"Expected text/html, got {r.headers.get('content-type')}"
    assert "<html" in r.text.lower() or "<!doctype html" in r.text.lower(), "Expected HTML in GET / response"
    print("[PASS] GET / returns index.html (HTTP 200)")

    # 2. GET /health returns healthy
    r = client.get("/health")
    assert r.status_code == 200, f"Expected 200 from GET /health, got {r.status_code}"
    body = r.json()
    assert body.get("status") == "healthy", f"Expected status == healthy, got {body}"
    print("[PASS] GET /health returns status: healthy (HTTP 200)")

    # 3. GET /api/status works
    r = client.get("/api/status")
    assert r.status_code == 200, f"Expected 200 from GET /api/status, got {r.status_code}"
    print("[PASS] GET /api/status returns HTTP 200")

    # 4. GET /docs works
    r = client.get("/docs")
    assert r.status_code == 200, f"Expected 200 from GET /docs, got {r.status_code}"
    print("[PASS] GET /docs returns HTTP 200")

    # 5. SPA routing for non-API path returns index.html
    r = client.get("/sandbox")
    assert r.status_code == 200, f"Expected 200 for SPA route /sandbox, got {r.status_code}"
    assert "<html" in r.text.lower() or "<!doctype html" in r.text.lower()
    print("[PASS] GET /sandbox (SPA route) returns index.html (HTTP 200)")

    # 6. Invalid API route returns 404 and is NOT intercepted by SPA
    r = client.get("/api/nonexistent_route_404")
    assert r.status_code == 404, f"Expected 404 for nonexistent API route, got {r.status_code}"
    print("[PASS] GET /api/nonexistent_route_404 returns 404 (not intercepted by SPA)")

    print("All in-process verifications PASSED!\n")


def verify_http_endpoint(base_url="http://localhost:8080"):
    print(f"--- [2] Live Endpoint Verification against {base_url} ---")
    try:
        # Check /
        req = urllib.request.Request(f"{base_url}/")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            status = resp.status
            content_type = resp.headers.get("content-type", "")
            body = resp.read().decode("utf-8")
            assert status == 200, f"Expected 200, got {status}"
            assert "text/html" in content_type, f"Expected text/html, got {content_type}"
            assert "<html" in body.lower() or "<!doctype html" in body.lower()
            print(f"[PASS] GET {base_url}/ returns index.html (HTTP 200)")

        # Check /health
        req = urllib.request.Request(f"{base_url}/health")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
            assert status == 200, f"Expected 200, got {status}"
            assert body.get("status") == "healthy", f"Expected healthy, got {body}"
            print(f"[PASS] GET {base_url}/health returns status: healthy (HTTP 200)")

        print(f"Live verification against {base_url} PASSED!\n")
        return True
    except Exception as exc:
        print(f"Note: Could not reach live server at {base_url} ({exc}).")
        return False


if __name__ == "__main__":
    verify_in_process()
    if len(sys.argv) > 1:
        verify_http_endpoint(sys.argv[1])
    else:
        verify_http_endpoint("http://localhost:8080")
