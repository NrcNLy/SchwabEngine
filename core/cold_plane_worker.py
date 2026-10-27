"""
core/cold_plane_worker.py
=========================
Workstream 1 (#124 / #130): Cold Plane Vertex AI Ingestion Pipeline & Signal Persistence.

Decouples asynchronous LLM macroeconomic synthesis from the hot execution loop via
atomic file-drop semantics to ``state/macro_prior.json``:
1. Validated schema contract: ``ControlSignalPayload``.
2. Time-aware model tier router: ``select_cold_plane_model``.
3. Atomic fsync-backed file-drop writer: ``write_control_signal_atomic``.
4. Non-blocking fault-tolerant hot-loop reader: ``read_control_signal``.
5. Autonomous synthesis orchestrator: ``ColdPlaneWorker``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from core.paths import MACRO_PRIOR_PATH, MACRO_PRIOR_FILE

logger = logging.getLogger("cold_plane_worker")
EDT = ZoneInfo("America/New_York")

ALLOWED_MODELS = ("gemini-4-argon", "gemini-3.8-flash")
ALLOWED_REGIMES = ("Goldilocks", "Uncertainty", "Macro Shock")
REQUIRED_PRIOR_KEYS = {"goldilocks", "uncertainty", "macro_shock"}


@dataclass
class ControlSignalPayload:
    """
    Schema contract for macroeconomic prior signals produced by the cold plane.
    Validated upon instantiation to protect the hot execution loop from corrupted state.
    """

    timestamp_utc: str
    model_version: str
    dominant_prior: str
    prior_probabilities: Dict[str, float]
    credit_spread_z_score: float
    nlp_sentiment_score: float
    satellite_target_weight: float
    circuit_breaker_override: bool

    def __post_init__(self) -> None:
        # 1. Validate timestamp
        if not isinstance(self.timestamp_utc, str) or not self.timestamp_utc.strip():
            raise ValueError(f"timestamp_utc must be a non-empty ISO-8601 string, got: {self.timestamp_utc}")
        try:
            ts_norm = self.timestamp_utc.replace("Z", "+00:00")
            parsed_ts = datetime.fromisoformat(ts_norm)
            if parsed_ts.tzinfo is None:
                raise ValueError("timestamp_utc must include timezone offset or Z")
        except Exception as exc:
            raise ValueError(f"Invalid timestamp_utc ISO-8601 string: {self.timestamp_utc}") from exc

        # 2. Validate model version
        if self.model_version not in ALLOWED_MODELS:
            raise ValueError(
                f"Invalid model_version: {self.model_version}. Must be one of {ALLOWED_MODELS}"
            )

        # 3. Validate dominant prior
        if self.dominant_prior not in ALLOWED_REGIMES:
            raise ValueError(
                f"Invalid dominant_prior: {self.dominant_prior}. Must be one of {ALLOWED_REGIMES}"
            )

        # 4. Validate prior probabilities dictionary
        if not isinstance(self.prior_probabilities, dict):
            raise ValueError("prior_probabilities must be a dictionary")
        if set(self.prior_probabilities.keys()) != REQUIRED_PRIOR_KEYS:
            raise ValueError(
                f"prior_probabilities must contain exactly keys {REQUIRED_PRIOR_KEYS}, got {set(self.prior_probabilities.keys())}"
            )
        prob_sum = 0.0
        for k, v in self.prior_probabilities.items():
            prob_val = float(v)
            if prob_val < 0.0 or prob_val > 1.0:
                raise ValueError(f"prior_probabilities['{k}'] = {prob_val} outside [0.0, 1.0]")
            prob_sum += prob_val
            self.prior_probabilities[k] = prob_val

        if abs(prob_sum - 1.0) > 1e-3:
            raise ValueError(f"prior_probabilities sum {prob_sum:.4f} deviates from 1.0 by > 1e-3")

        # 5. Types & Bounds
        self.credit_spread_z_score = float(self.credit_spread_z_score)
        self.nlp_sentiment_score = max(-1.0, min(1.0, float(self.nlp_sentiment_score)))
        self.satellite_target_weight = max(0.0, min(0.35, float(self.satellite_target_weight)))
        self.circuit_breaker_override = bool(self.circuit_breaker_override)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize payload to a standard dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ControlSignalPayload":
        """Construct and validate a ControlSignalPayload from dictionary data."""
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for ControlSignalPayload, got {type(data).__name__}")
        return cls(
            timestamp_utc=str(data["timestamp_utc"]),
            model_version=str(data["model_version"]),
            dominant_prior=str(data["dominant_prior"]),
            prior_probabilities={
                k: float(v) for k, v in data["prior_probabilities"].items()
            },
            credit_spread_z_score=float(data["credit_spread_z_score"]),
            nlp_sentiment_score=float(data["nlp_sentiment_score"]),
            satellite_target_weight=float(data["satellite_target_weight"]),
            circuit_breaker_override=bool(data["circuit_breaker_override"]),
        )


def select_cold_plane_model(now_et: Optional[datetime] = None) -> str:
    """
    Tier-based model router for Cold Plane synthesis.

    - Pre-Market Macro Prior Gate (09:10:00 - 09:25:00 EDT) and EOD synthesis (>= 15:55:00 EDT):
      Routes to flagship high-reasoning model 'gemini-4-argon'.
    - Intraday periodic synthesis (09:30:00 - 15:50:00 EDT):
      Routes to low-latency cost-effective model 'gemini-3.8-flash'.
    """
    if now_et is None:
        now_et = datetime.now(EDT)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=EDT)
    else:
        now_et = now_et.astimezone(EDT)

    t = now_et.time()
    if (dt_time(9, 10, 0) <= t <= dt_time(9, 25, 0)) or (t >= dt_time(15, 55, 0)):
        return "gemini-4-argon"
    return "gemini-3.8-flash"


def write_control_signal_atomic(
    payload: ControlSignalPayload,
    target_path: Path = MACRO_PRIOR_PATH,
) -> Path:
    """
    Atomically write serialized ControlSignalPayload JSON to disk.
    Uses sibling tempfile + fsync + os.replace to guarantee zero torn reads by hot loop.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(".json.tmp")

    serialized = json.dumps(payload.to_dict(), indent=2)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(serialized)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, target_path)
    logger.debug("Persisted control signal atomically to %s", target_path)
    return target_path


write_macro_prior_atomic = write_control_signal_atomic


def read_control_signal(
    target_path: Path = MACRO_PRIOR_PATH,
    max_age_seconds: float = 3600.0,
    now_utc: Optional[datetime] = None,
) -> Optional[ControlSignalPayload]:
    """
    Read and validate control signal from disk with staleness and corruption guards.
    Never raises exceptions into hot loop; returns None on missing, corrupt, or stale signals.
    """
    target_path = Path(target_path)
    if not target_path.exists():
        logger.debug("Control signal file does not exist: %s", target_path)
        return None

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        payload = ControlSignalPayload.from_dict(raw_data)

        # Check staleness
        ts_norm = payload.timestamp_utc.replace("Z", "+00:00")
        sig_time = datetime.fromisoformat(ts_norm)
        if sig_time.tzinfo is None:
            sig_time = sig_time.replace(tzinfo=timezone.utc)

        current_utc = now_utc if now_utc is not None else datetime.now(timezone.utc)
        if current_utc.tzinfo is None:
            current_utc = current_utc.replace(tzinfo=timezone.utc)

        age_seconds = (current_utc - sig_time).total_seconds()
        if age_seconds > float(max_age_seconds):
            logger.warning(
                "Cold plane signal stale (age=%.1fs > max_age=%.1fs, ts=%s, path=%s)",
                age_seconds,
                max_age_seconds,
                payload.timestamp_utc,
                target_path,
            )
            return None

        return payload

    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.warning("Failed to read or validate cold plane signal from %s: %s", target_path, exc)
        return None


read_macro_prior = read_control_signal


class ColdPlaneWorker:
    """
    Worker orchestrator for periodic macroeconomic and news synthesis.
    Executes synthesis cycles, formats payloads, and writes atomic control signals.
    """

    def __init__(
        self,
        llm_provider: Optional[Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
        signal_path: Path = MACRO_PRIOR_PATH,
    ) -> None:
        self.llm_provider = llm_provider
        self.signal_path = Path(signal_path)

    async def execute_synthesis_cycle(
        self,
        market_context: Dict[str, Any],
        now_et: Optional[datetime] = None,
    ) -> ControlSignalPayload:
        """
        Execute one cold plane synthesis cycle.
        Invokes LLM provider if configured, or falls back to deterministic quantitative heuristics.
        Atomically persists the resulting ControlSignalPayload to self.signal_path.
        """
        model = select_cold_plane_model(now_et)
        now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if self.llm_provider is not None:
            raw_output = await self.llm_provider(model, market_context)
            payload = ControlSignalPayload(
                timestamp_utc=str(raw_output.get("timestamp_utc", now_utc_str)),
                model_version=model,
                dominant_prior=str(raw_output.get("dominant_prior", "Uncertainty")),
                prior_probabilities=dict(raw_output.get("prior_probabilities", {
                    "goldilocks": 0.20,
                    "uncertainty": 0.60,
                    "macro_shock": 0.20,
                })),
                credit_spread_z_score=float(raw_output.get("credit_spread_z_score", 0.0)),
                nlp_sentiment_score=float(raw_output.get("nlp_sentiment_score", 0.0)),
                satellite_target_weight=float(raw_output.get("satellite_target_weight", 0.15)),
                circuit_breaker_override=bool(raw_output.get("circuit_breaker_override", False)),
            )
        else:
            # Deterministic quantitative heuristic fallback
            cs_z = float(market_context.get("credit_spread_z_score", 0.0))
            qqq_vel = float(market_context.get("qqq_velocity_15m", 0.0))
            sentiment = float(market_context.get("nlp_sentiment_score", 0.0))

            if cs_z >= 2.0 or qqq_vel <= -0.015:
                dominant = "Macro Shock"
                prior_probs = {"goldilocks": 0.05, "uncertainty": 0.25, "macro_shock": 0.70}
                sat_weight = 0.0
                cb = (cs_z >= 2.5 or qqq_vel <= -0.02)
            elif cs_z <= 0.5 and sentiment >= 0.0 and qqq_vel >= 0.0:
                dominant = "Goldilocks"
                prior_probs = {"goldilocks": 0.70, "uncertainty": 0.20, "macro_shock": 0.10}
                sat_weight = 0.35
                cb = False
            else:
                dominant = "Uncertainty"
                prior_probs = {"goldilocks": 0.20, "uncertainty": 0.60, "macro_shock": 0.20}
                sat_weight = 0.15
                cb = False

            payload = ControlSignalPayload(
                timestamp_utc=now_utc_str,
                model_version=model,
                dominant_prior=dominant,
                prior_probabilities=prior_probs,
                credit_spread_z_score=cs_z,
                nlp_sentiment_score=sentiment,
                satellite_target_weight=sat_weight,
                circuit_breaker_override=cb,
            )

        write_control_signal_atomic(payload, self.signal_path)
        return payload


async def run_cold_plane_cycle(
    signal_path: Path = MACRO_PRIOR_PATH,
    now_et: Optional[datetime] = None,
) -> ControlSignalPayload:
    """CLI and autonomous entrypoint for periodic cold plane macro synthesis."""
    from dotenv import load_dotenv
    load_dotenv()

    if now_et is None:
        now_et = datetime.now(EDT)

    context: Dict[str, Any] = {}

    # Ingest SES snapshot if available
    ses_file = Path("data/exports/latest_ses_snapshot.json")
    if ses_file.exists():
        try:
            with open(ses_file, "r", encoding="utf-8") as f:
                ses_data = json.load(f)
                ses_score = float(ses_data.get("ses_score", 0.0))
                context["nlp_sentiment_score"] = float(ses_score)
        except Exception as exc:
            logger.debug("Could not read SES snapshot: %s", exc)

    worker = ColdPlaneWorker(signal_path=signal_path)
    payload = await worker.execute_synthesis_cycle(context, now_et=now_et)
    logger.info(
        "ColdPlaneWorker: cycle complete -> model=%s, dominant=%s, w_sat=%.2f, cb_override=%s, path=%s",
        payload.model_version,
        payload.dominant_prior,
        payload.satellite_target_weight,
        payload.circuit_breaker_override,
        signal_path,
    )
    return payload


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    asyncio.run(run_cold_plane_cycle())


if __name__ == "__main__":
    main()

