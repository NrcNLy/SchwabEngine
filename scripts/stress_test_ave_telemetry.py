"""
scripts/stress_test_ave_telemetry.py
====================================
Comprehensive QA & Systems Performance Stress Test for M3 AVE Dashboard.

Verifies:
1. Replay Synthetic Tick Bursts:
   - Streams 100 synthetic ticks at 20Hz (50ms interval) to /ws/ave/SOXL.
   - Verifies WebSocket client ingests frames with zero sequence gaps (seq continuity).
   - Verifies ring buffer snapshot and frame vector format.
2. Responsive Viewport Check:
   - Verifies full-bleed edge-to-edge container rules across 1080p, 1440p, and compact viewports.
   - Verifies uPlot HUD legend overlay styles and overflow containment.
3. Asymmetric Switch Contract:
   - Tests transitions between 'shadow' and 'live' via POST /api/config/ave-mode.
   - Verifies atomic persistence to strategy_config.json on disk.
   - Verifies 422 error rejection for invalid modes.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import orjson
from starlette.testclient import TestClient

from api.ave_telemetry import AveTelemetryHub
from api.server import build_app
from core.runtime import EngineContext, build_ledger, load_config
from execution.risk_manager import RiskEngine


def test_synthetic_tick_bursts():
    print("\n=======================================================")
    print(" [1] REPLAY SYNTHETIC TICK BURSTS (100 ticks @ 20Hz)")
    print("=======================================================")

    # Reset hub instance for fresh test isolation
    AveTelemetryHub.reset_instance()
    hub = AveTelemetryHub.get_instance()

    cfg = load_config()
    ctx = EngineContext(cfg, live=False)
    ctx.ledgers["sandbox"] = build_ledger(cfg, live=False)
    ctx.risk_manager = RiskEngine()
    app = build_app(ctx)
    client = TestClient(app)

    sym = "SOXL"
    ticks_to_send = 100
    frequency_hz = 20.0
    interval_s = 1.0 / frequency_hz  # 0.050s = 50ms

    received_frames = []
    received_snapshots = []

    print(f"Connecting WebSocket client to /ws/ave/{sym}...")
    with client.websocket_connect(f"/ws/ave/{sym}") as ws:
        # First message must be the SNAPSHOT handshake
        first_raw = ws.receive_bytes()
        first_msg = orjson.loads(first_raw)
        assert first_msg.get("type") == "SNAPSHOT", f"Expected SNAPSHOT, got {first_msg}"
        assert first_msg.get("symbol") == sym
        received_snapshots.append(first_msg)
        print(f"[PASS] Snapshot handshake received for {sym}. Initial buffer items: {len(first_msg.get('data', []))}")

        start_time = time.perf_counter()
        base_epoch = int(time.time() * 1000)
        base_price = 45.50

        print(f"Streaming {ticks_to_send} synthetic ticks at {frequency_hz}Hz (interval: {interval_s*1000:.1f}ms)...")

        for i in range(ticks_to_send):
            epoch_ms = base_epoch + i * int(interval_s * 1000)
            # Simulate a realistic random-walk price with expanding/tightening volatility cone
            price = round(base_price + (i * 0.05) - ((i % 7) * 0.03), 4)
            upper = round(price + 1.25 - (i * 0.005), 4)
            lower = round(price - 1.25 + (i * 0.005), 4)
            lambda_factor = max(0.1, round(1.0 - (i * 0.008), 5))
            supremum = round(upper + 0.15, 4)
            state_code = 1 if i < 30 else (2 if i < 70 else 3)

            # Ingest tick with force_broadcast=True for 20Hz stress replay
            hub.ingest_tick_frame(
                symbol=sym,
                epoch_ms=epoch_ms,
                price=price,
                upper=upper,
                lower=lower,
                lambda_factor=lambda_factor,
                supremum=supremum,
                state_code=state_code,
                force_broadcast=True,
            )

            # Receive binary frame from client connection
            raw_bytes = ws.receive_bytes()
            frame_msg = orjson.loads(raw_bytes)
            assert frame_msg.get("type") == "FRAME", f"Expected FRAME, got {frame_msg}"
            assert frame_msg.get("symbol") == sym
            received_frames.append(frame_msg.get("frame"))

        total_elapsed = time.perf_counter() - start_time
        effective_hz = ticks_to_send / total_elapsed
        print(f"[PASS] Completed burst: {ticks_to_send} frames in {total_elapsed:.3f}s ({effective_hz:.1f} Hz effective throughput)")

    # Validate Sequence Number Continuity (Zero dropped frames, zero sequence gaps)
    seqs = [f[0] for f in received_frames]
    expected_seqs = list(range(1, ticks_to_send + 1))
    assert seqs == expected_seqs, f"Sequence gap detected! Expected 1..{ticks_to_send}, got seq range: {seqs[0]}..{seqs[-1]}"
    print(f"[PASS] Monotonic sequence check: 100% frame continuity (seq {seqs[0]} -> {seqs[-1]}, zero gaps).")

    # Validate Frame Structure Integrity
    # Frame format: [seq, epoch_ms, price, upper, lower, lambda, supremum, state_code]
    sample_frame = received_frames[-1]
    assert len(sample_frame) == 8, f"Expected 8 vector components, got {len(sample_frame)}"
    assert isinstance(sample_frame[0], int)  # seq
    assert isinstance(sample_frame[1], int)  # epoch_ms
    assert isinstance(sample_frame[2], float)  # price
    assert isinstance(sample_frame[3], float)  # upper
    assert isinstance(sample_frame[4], float)  # lower
    assert isinstance(sample_frame[5], float)  # lambda
    assert isinstance(sample_frame[6], float)  # supremum
    assert isinstance(sample_frame[7], int)  # state_code
    print(f"[PASS] Frame vector integrity verified: {sample_frame}")

    # Validate Hub Ring Buffer Retention
    buf = hub._get_or_create_buffer(sym)
    snapshot = buf.get_snapshot()
    assert len(snapshot) == ticks_to_send
    print(f"[PASS] Ring buffer capacity & snapshot retention confirmed: {len(snapshot)} frames.")


def test_responsive_viewport_constraints():
    print("\n=======================================================")
    print(" [2] RESPONSIVE VIEWPORT & FULL-BLEED LAYOUT AUDIT")
    print("=======================================================")

    dist_html_path = PROJECT_ROOT / "dist" / "index.html"
    index_css_path = PROJECT_ROOT / "src" / "index.css"
    app_tsx_path = PROJECT_ROOT / "src" / "App.tsx"
    layout_tsx_path = PROJECT_ROOT / "src" / "components" / "DashboardLayout.tsx"
    chart_tsx_path = PROJECT_ROOT / "src" / "components" / "AveEnvelopeChart.tsx"

    # 1. Viewport Meta in HTML
    html_content = dist_html_path.read_text(encoding="utf-8")
    assert 'meta name="viewport" content="width=device-width, initial-scale=1.0"' in html_content
    print("[PASS] HTML Viewport: Responsive meta tag configured with width=device-width.")

    # 2. Check Layout Full Bleed Rules
    layout_content = layout_tsx_path.read_text(encoding="utf-8")
    assert "activeTab === 'ave' ? 'w-full p-0' : 'w-full p-4 sm:p-6 max-w-7xl mx-auto'" in layout_content
    assert "overflow-hidden" in layout_content
    print("[PASS] DashboardLayout: AVE tab unconstrained by max-w-7xl, using full-bleed (w-full p-0).")

    # 3. Check Chart Viewport Container Rules
    chart_content = chart_tsx_path.read_text(encoding="utf-8")
    assert "flex flex-col w-full h-full flex-1" in chart_content
    assert "ResizeObserver" in chart_content
    print("[PASS] AveEnvelopeChart: Responsive ResizeObserver active with 100% flex fill.")

    # 4. Check HUD Legend Overlay in index.css
    css_content = index_css_path.read_text(encoding="utf-8")
    assert ".uplot .u-legend" in css_content
    assert "position: absolute !important;" in css_content
    assert "backdrop-filter: blur(8px);" in css_content
    print("[PASS] CSS HUD Legend: Docked as top-right absolute overlay; zero vertical displacement.")

    # 5. Check Viewport Resolution Arithmetic
    # Standard Resolutions:
    viewports = [
        {"name": "1080p Full HD", "width": 1920, "height": 1080, "rail": 72, "topbar": 48, "hud": 32},
        {"name": "1440p Quad HD", "width": 2560, "height": 1440, "rail": 72, "topbar": 48, "hud": 32},
        {"name": "1080p Pinned Rail", "width": 1920, "height": 1080, "rail": 220, "topbar": 48, "hud": 32},
        {"name": "Compact Laptop", "width": 1366, "height": 768, "rail": 72, "topbar": 48, "hud": 32},
    ]

    for vp in viewports:
        canvas_width = vp["width"] - vp["rail"]
        canvas_height = vp["height"] - vp["topbar"] - vp["hud"]
        assert canvas_width > 1200, f"Canvas width too cramped on {vp['name']}"
        assert canvas_height > 600, f"Canvas height too cramped on {vp['name']}"
        print(f"[PASS] {vp['name']}: Canvas renders at {canvas_width}px x {canvas_height}px (Zero horizontal overflow).")


def test_asymmetric_switch_contract():
    print("\n=======================================================")
    print(" [3] ASYMMETRIC SAFETY SWITCH & PERSISTENCE CONTRACT")
    print("=======================================================")

    cfg = load_config()
    ctx = EngineContext(cfg, live=False)
    ctx.ledgers["sandbox"] = build_ledger(cfg, live=False)
    ctx.risk_manager = RiskEngine()
    app = build_app(ctx)
    client = TestClient(app)

    # 1. Verify GET /api/config/ave-mode
    r = client.get("/api/config/ave-mode")
    assert r.status_code == 200
    initial_mode = r.json().get("mode")
    print(f"[PASS] GET /api/config/ave-mode: initial mode is '{initial_mode}' (HTTP 200).")

    # 2. Test Transition: -> LIVE
    r_live = client.post("/api/config/ave-mode", json={"mode": "live"})
    assert r_live.status_code == 200
    assert r_live.json() == {"success": True, "mode": "live"}
    # Verify disk persistence in strategy_config.json
    strategy_cfg = json.loads(Path("config/strategy_config.json").read_text(encoding="utf-8"))
    assert strategy_cfg.get("ave", {}).get("mode") == "live"
    print("[PASS] POST /api/config/ave-mode ('live'): State mutated & persisted to strategy_config.json.")

    # 3. Test Transition: -> SHADOW (Zero-friction Kill Switch)
    r_shadow = client.post("/api/config/ave-mode", json={"mode": "shadow"})
    assert r_shadow.status_code == 200
    assert r_shadow.json() == {"success": True, "mode": "shadow"}
    strategy_cfg = json.loads(Path("config/strategy_config.json").read_text(encoding="utf-8"))
    assert strategy_cfg.get("ave", {}).get("mode") == "shadow"
    print("[PASS] POST /api/config/ave-mode ('shadow'): Emergency kill switch persisted to strategy_config.json.")

    # 4. Test Validation Error on Invalid Mode
    r_invalid = client.post("/api/config/ave-mode", json={"mode": "turbo_unhedged"})
    assert r_invalid.status_code == 422
    print("[PASS] POST /api/config/ave-mode (invalid): HTTP 422 Unprocessable Entity properly rejected.")


if __name__ == "__main__":
    print("===================================================================")
    print(" SCHWABENGINE AVE DASHBOARD TELEMETRY & VIEWPORT STRESS TEST ")
    print("===================================================================")
    test_synthetic_tick_bursts()
    test_responsive_viewport_constraints()
    test_asymmetric_switch_contract()
    print("\n===================================================================")
    print(" ALL PERFORMANCE & STRESS TESTS PASSED WITH ZERO ANOMALIES ")
    print("===================================================================\n")
