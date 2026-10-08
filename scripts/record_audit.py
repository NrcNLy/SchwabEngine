"""
scripts/record_audit.py
Records the hotpatch deployment event into SQLite WAL telemetry.
"""

from datetime import datetime, timezone
from core.telemetry import SQLiteWALEventStore, TelemetryEvent

def record():
    store = SQLiteWALEventStore("/app/data/schwab_telemetry.db")
    event = TelemetryEvent(
        aggregate_id="ENGINE_HOTPATCH",
        event_type="HOTPATCH_DEPLOYED",
        payload={
            "fix": "dataclasses.FrozenInstanceError on TradeSignal in execution/strategies.py",
            "details": "Replaced direct in-place mutation of signal.quantity with dataclasses.replace(signal, quantity=clamped_quantity)",
            "target_file": "/app/execution/strategies.py",
            "deployed_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "VERIFIED_ACTIVE"
        }
    )
    store.append_event(event)
    print("Audit log recorded successfully: event_id =", event.event_id)

if __name__ == "__main__":
    record()
