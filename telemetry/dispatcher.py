"""
telemetry/dispatcher.py
=======================
Firebase Cloud Messaging (FCM) telemetry dispatcher.
Broadcasts lightweight JSON event payloads to the operator's Android device.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)


class TelemetryDispatcher:
    """
    Constructs and sends high-priority data messages via FCM.
    """

    def __init__(self, fcm_url: str, fcm_server_key: str, device_token: str, enabled: bool = True):
        self._fcm_url = fcm_url
        self._fcm_server_key = fcm_server_key
        self._device_token = device_token
        self._enabled = enabled

    @classmethod
    def from_config(cls, cfg: dict) -> "TelemetryDispatcher":
        tel_cfg = cfg.get("telemetry", {})
        return cls(
            fcm_url=tel_cfg.get("fcm_url", "https://fcm.googleapis.com/fcm/send"),
            fcm_server_key=tel_cfg.get("fcm_server_key", ""),
            device_token=tel_cfg.get("device_token", ""),
            enabled=tel_cfg.get("enabled", True),
        )

    def _send(self, event_type: str, payload: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        if not self._fcm_server_key or not self._device_token:
            logger.debug("TelemetryDispatcher: Missing FCM keys. Cannot send %s", event_type)
            return

        headers = {
            "Authorization": f"key={self._fcm_server_key}",
            "Content-Type": "application/json",
        }

        # FCM high-priority data message format
        data = {
            "to": self._device_token,
            "priority": "high",
            "data": {
                "event_type": event_type,
                "timestamp": str(int(time.time())),
                **{k: str(v) for k, v in payload.items()}  # FCM data values must be strings
            },
        }

        try:
            resp = requests.post(self._fcm_url, headers=headers, json=data, timeout=5)
            if resp.status_code == 200:
                logger.info("Telemetry: Sent %s notification successfully.", event_type)
            else:
                logger.warning(
                    "Telemetry: FCM HTTP %d on %s: %s",
                    resp.status_code, event_type, resp.text
                )
        except requests.RequestException as exc:
            logger.error("Telemetry: Network error sending %s: %s", event_type, exc)

    def send_pre_market_diagnostic(self, system_state: Dict[str, Any]) -> None:
        """Broadcast daily initialization state (e.g., account balance, universe)."""
        self._send("PRE_MARKET_DIAGNOSTIC", system_state)

    def send_signal_generated(self, signal_data: Dict[str, Any]) -> None:
        """Broadcast when a trade signal passes the MacroGatekeeper."""
        self._send("SIGNAL_GENERATED", signal_data)

    def send_order_lifecycle(self, order_data: Dict[str, Any]) -> None:
        """Broadcast fills, partials, or cancellations."""
        self._send("ORDER_LIFECYCLE", order_data)

    def send_risk_breach(self, breach_data: Dict[str, Any]) -> None:
        """Broadcast when Tier 1 or Tier 2 stops are hit."""
        self._send("RISK_BREACH", breach_data)

    def send_portfolio_sweep(self, sweep_data: Dict[str, Any]) -> None:
        """Broadcast the 15:55 EDT EOD liquidation sweep event."""
        self._send("PORTFOLIO_SWEEP", sweep_data)
