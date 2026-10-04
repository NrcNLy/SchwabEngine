import sys
import os
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.server import build_app, EngineContext
from fastapi.testclient import TestClient

ctx = EngineContext()
app = build_app(ctx)
client = TestClient(app)

def run_tests():
    print("Testing GET /health")
    resp = client.get("/health")
    print(f"Status: {resp.status_code}")
    print(f"JSON: {json.dumps(resp.json(), indent=2)}")
    assert resp.status_code == 200
    
    print("\nTesting POST /auth/refresh")
    resp = client.post("/auth/refresh")
    print(f"Status: {resp.status_code}")
    print(f"JSON: {json.dumps(resp.json(), indent=2)}")
    assert resp.status_code == 200
    
    print("\nTesting POST /emergency/halt")
    resp = client.post("/emergency/halt", json={"halted": True})
    print(f"Status: {resp.status_code}")
    print(f"JSON: {json.dumps(resp.json(), indent=2)}")
    assert resp.status_code == 200
    
    print("\nTesting POST /emergency/liquidate")
    resp = client.post("/emergency/liquidate")
    print(f"Status: {resp.status_code}")
    print(f"JSON: {json.dumps(resp.json(), indent=2)}")
    assert resp.status_code == 200
    
    print("\nAll tests passed.")

if __name__ == "__main__":
    run_tests()
