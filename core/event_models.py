"""
core/event_models.py
====================
Structured Pydantic V2 Domain Event Models for Zero-Allocation Telemetry (#67).

Defines strongly-typed, immutable events recorded by the asynchronous event-sourcing ledger:
- SignalGeneratedEvent: Generated algorithmic entry signals with macro/micro metrics.
- OrderRoutedEvent: Broker-facing order submissions with limit and notional values.
- ExecutionFilledEvent: Order fill executions with slippage and exchange fees.
- SentinelRatchetEvent: Synthetic microstructure stop-loss ratchet and watermark updates.
- SettlementUpdatedEvent: Account cash settlement, NLV, and Good Faith Violation states.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator


class BaseEngineEvent(BaseModel):
    """
    Abstract domain event base class with zero-overhead serialization helpers.
    """
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @property
    def event_type(self) -> str:
        """Class name representing the event type."""
        return self.__class__.__name__

    @property
    def timestamp_utc(self) -> str:
        """ISO 8601 UTC string representation of the event timestamp."""
        ts = getattr(self, "timestamp", None)
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(timezone.utc).isoformat()
        if isinstance(ts, str):
            return ts
        return datetime.now(timezone.utc).isoformat()

    def get_symbol(self) -> Optional[str]:
        """Extract symbol if present, or None."""
        return getattr(self, "symbol", None)

    def to_payload_dict(self) -> Dict[str, Any]:
        """Convert model fields to JSON-serializable dictionary."""
        return self.model_dump(mode="json")

    def to_payload_json(self) -> str:
        """Serialize model fields to JSON string."""
        return self.model_dump_json()


class SignalGeneratedEvent(BaseEngineEvent):
    """
    Recorded when an algorithmic strategy generates a trade signal candidate.
    """
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    symbol: str
    strategy: str
    rvol: float
    ses_score: float
    s0_price: float

    @field_validator("timestamp", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v: Any) -> str:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc).isoformat()
        return str(v)


class OrderRoutedEvent(BaseEngineEvent):
    """
    Recorded when an order wire payload is submitted to the Schwab API or broker gateway.
    """
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    client_order_id: str
    symbol: str
    action: str
    shares: int
    limit_price: float
    notional: float

    @field_validator("timestamp", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v: Any) -> str:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc).isoformat()
        return str(v)


class ExecutionFilledEvent(BaseEngineEvent):
    """
    Recorded upon receiving execution fill telemetry from the broker wire.
    """
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    order_id: str
    symbol: str
    fill_price: float
    slippage_usd: float = 0.0
    fee_usd: float = 0.0

    @field_validator("timestamp", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v: Any) -> str:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc).isoformat()
        return str(v)


class SentinelRatchetEvent(BaseEngineEvent):
    """
    Recorded when the synthetic microstructure sentinel ratchets a trailing stop
    or high-watermark level.
    """
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    symbol: str
    high_watermark: float
    dynamic_trail_price: float
    floor_price: float

    @field_validator("timestamp", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v: Any) -> str:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc).isoformat()
        return str(v)


class SettlementUpdatedEvent(BaseEngineEvent):
    """
    Recorded when cash balances, settled funds, and settlement violation states update.
    """
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    settled_cash: float
    unsettled_cash: float
    total_nlv: float
    gfv_count: int = 0
    symbol: Optional[str] = "ACCOUNT"

    @field_validator("timestamp", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v: Any) -> str:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc).isoformat()
        return str(v)


# Polymorphic domain event union
EngineEvent = Union[
    SignalGeneratedEvent,
    OrderRoutedEvent,
    ExecutionFilledEvent,
    SentinelRatchetEvent,
    SettlementUpdatedEvent,
    BaseEngineEvent,
]
