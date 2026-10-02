"""
macro/gatekeeper.py
===================
LLM Macroeconomic Gatekeeper — prevents algorithmic execution during
scheduled data releases or qualitative news shocks.

Components
----------
MacroDecisionPayload
    Pydantic schema for Gemini structured output (exact spec from research doc).
    Fields: macro_regime, market_bias, confidence_score, risk_multiplier,
            trade_permitted, inhibition_reason, high_impact_warning.

MacroGatekeeper
    Two-layer veto mechanism:

    Layer 1 — Temporal Lockout (hard-coded EDT time windows):
        08:25:00–08:45:00  All weekdays  CPI / PPI / PCE / NFP / GDP
        13:55:00–15:30:00  All weekdays  FOMC Rate Decision / Press Conference
        10:25:00–10:45:00  Thursdays     EIA NatGas (BOIL symbol only)

    Layer 2 — JIT LLM Gatekeeper (Just-In-Time on each trade signal):
        1. Check MacroSemanticCache for a similar recent decision (cosine ≥ 0.92,
           age ≤ 30 min) — return cached result immediately on hit.
        2. Acquire a token from the 8-RPM LLM Token Bucket (rate limit gate).
        3. Call Gemini 2.5 Flash with a 1500ms hard timeout.
        4. On success: parse MacroDecisionPayload, store in semantic cache.
        5. On timeout or HTTP 429: apply degradation policy
               trade_permitted=True, risk_multiplier=0.50

Configuration sources (config.yaml):
    macro.gemini_model              → "gemini-2.5-flash"
    macro.llm_timeout_ms            → 1500
    macro.llm_rpm_limit             → 8
    macro.lockout_windows           → list of lockout dicts
    macro.semantic_cache.*          → forwarded to MacroSemanticCache
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from pydantic import BaseModel, Field

from core.rate_limiter import TokenBucket
from macro.semantic_cache import MacroSemanticCache

logger = logging.getLogger(__name__)

_EDT = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# Degradation constants
# ---------------------------------------------------------------------------
_DEGRADED_RISK_MULTIPLIER   = 0.50   # Per spec: reduce size on degradation
_DEGRADED_TRADE_PERMITTED   = True   # Per spec: allow trade but at reduced risk
_BACKOFF_INITIAL_SEC        = 1.0
_BACKOFF_MAX_SEC            = 16.0
_MAX_RETRIES_429            = 3


# ===========================================================================
# MacroDecisionPayload — exact schema from research doc
# ===========================================================================

class MacroDecisionPayload(BaseModel):
    """
    Structured output schema for Gemini 2.5 Flash macro evaluation.

    Reproduced verbatim from the research specification (Part 4):

        macro_regime:       Current macro regime label (e.g. "RISK_OFF", "NEUTRAL").
        market_bias:        Qualitative directional bias ("BULLISH"/"BEARISH"/"NEUTRAL").
        confidence_score:   LLM confidence in the assessment [0.0–1.0].
        risk_multiplier:    Multiplicative scale for position sizing [0.0–1.0].
                            1.0 = full size, 0.5 = half size, 0.0 = no trade.
        trade_permitted:    True if the LLM approves execution; False = veto.
        inhibition_reason:  Human-readable reason if trade_permitted=False. None otherwise.
        high_impact_warning: Any critical data release or news event to flag. None if none.
    """
    macro_regime:        str
    market_bias:         str
    confidence_score:    float
    risk_multiplier:     float
    trade_permitted:     bool
    inhibition_reason:   str | None = None
    high_impact_warning: str | None = None


# ===========================================================================
# LockoutWindow — parsed representation of a single config lockout block
# ===========================================================================

class _LockoutWindow:
    """Internal representation of a single temporal lockout rule."""

    def __init__(self, raw: dict) -> None:
        self.name:    str       = raw.get("name", "Unknown Lockout")
        self.days:    List[str] = raw.get("days", [])
        self.symbols: List[str] = [s.upper() for s in raw.get("symbols", [])]

        start_str = raw.get("start", "00:00:00")
        end_str   = raw.get("end",   "00:00:00")

        s = [int(x) for x in start_str.split(":")]
        e = [int(x) for x in end_str.split(":")]
        self.start_hms: Tuple[int, int, int] = (s[0], s[1], s[2])
        self.end_hms:   Tuple[int, int, int] = (e[0], e[1], e[2])

    def is_active(self, symbol: str, now: datetime) -> bool:
        """
        Return True if this lockout window is currently active for the given symbol.

        Args:
            symbol: The trading symbol (uppercase).
            now:    Current datetime in EDT.
        """
        # Day-of-week check
        day_name = now.strftime("%A")   # "Monday", "Tuesday", ...
        if self.days and day_name not in self.days:
            return False

        # Symbol-specific lockout: if symbols list is non-empty,
        # only block that specific symbol (e.g. BOIL for EIA NatGas)
        if self.symbols and symbol.upper() not in self.symbols:
            return False

        # Time window check (seconds-precise comparison)
        now_seconds = now.hour * 3600 + now.minute * 60 + now.second
        start_sec   = self.start_hms[0] * 3600 + self.start_hms[1] * 60 + self.start_hms[2]
        end_sec     = self.end_hms[0]   * 3600 + self.end_hms[1]   * 60 + self.end_hms[2]

        return start_sec <= now_seconds < end_sec


# ===========================================================================
# MacroGatekeeper
# ===========================================================================

class MacroGatekeeper:
    """
    Two-layer macroeconomic execution gatekeeper.

    Layer 1 — Temporal Lockout:
        Hard-coded EDT time windows that immediately veto trades without
        any API calls. Fires are instantaneous and deterministic.

    Layer 2 — JIT LLM Evaluation:
        On each trade signal (after passing Layer 1), queries Gemini 2.5 Flash
        with the signal context. Checks semantic cache first; calls the LLM
        only on a cache miss.

    The `evaluate()` method is the single public entry point.
    It always returns a MacroDecisionPayload — never raises an exception.
    """

    def __init__(
        self,
        cfg:   dict,
        cache: MacroSemanticCache,
    ) -> None:
        """
        Args:
            cfg:   Top-level config dict loaded from config.yaml.
            cache: Initialised MacroSemanticCache instance.
        """
        macro_cfg = cfg.get("macro", {})

        self._model:       str   = macro_cfg.get("gemini_model", "gemini-2.5-flash")
        self._timeout_sec: float = macro_cfg.get("llm_timeout_ms", 1500) / 1000.0
        self._rpm_limit:   int   = int(macro_cfg.get("llm_rpm_limit", 8))
        self._cache                = cache

        # Parse lockout windows from config
        self._lockouts: List[_LockoutWindow] = [
            _LockoutWindow(w)
            for w in macro_cfg.get("lockout_windows", [])
        ]

        # 8-RPM token bucket for the Gemini API
        # refill_rate = rpm / 60 (tokens per second)
        self._llm_bucket = TokenBucket(
            capacity=float(self._rpm_limit),
            refill_rate=self._rpm_limit / 60.0,
        )

        # Lazy-initialised Gemini client (avoids import error if key not set at startup)
        self._gemini_client = None
        self._gemini_lock   = threading.Lock()

        # Thread pool for 1500ms timeout enforcement
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="GeminiCall",
        )

        logger.info(
            "MacroGatekeeper initialised — model=%s, timeout=%.0fms, "
            "rpm_limit=%d, lockout_windows=%d",
            self._model, self._timeout_sec * 1000,
            self._rpm_limit, len(self._lockouts),
        )

        # Decision log for future model distillation
        # Each LLM evaluation is appended as a JSONL record to
        # /app/logs/macro_decisions.jsonl for offline analysis.
        log_dir = Path(macro_cfg.get("decision_log_dir", "/app/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        self._decision_log_path = log_dir / "macro_decisions.jsonl"
        self._decision_log_lock = threading.Lock()
        logger.info(
            "MacroGatekeeper: decision log → %s", self._decision_log_path
        )

    # ------------------------------------------------------------------
    # Primary evaluation entry point
    # ------------------------------------------------------------------

    def evaluate(
        self,
        signal_context: Dict[str, Any],
        headline: Optional[str] = None,
    ) -> MacroDecisionPayload:
        """
        Evaluate whether a trade signal should be permitted to proceed.

        Execution flow:
            1. Temporal lockout check → immediate veto if in a lockout window.
            2. Semantic cache lookup → return cached payload on hit.
            3. LLM Token Bucket acquire → rate-limit gate.
            4. Gemini API call (1500ms timeout).
            5. Parse response → store in cache → return payload.
            6. On timeout / HTTP 429 → degradation policy.

        Args:
            signal_context: Dict with signal metadata (symbol, strategy,
                            entry_price, regime, etc.) used to build the prompt.
            headline:       Optional news headline to embed in the cache lookup
                            and Gemini prompt. If None, a generic prompt is used.

        Returns:
            MacroDecisionPayload — always populated; never raises.
        """
        symbol = str(signal_context.get("symbol", "")).upper()
        now    = datetime.now(_EDT)

        # --- Layer 1: Temporal Lockout ---
        locked, lockout_name = self.is_locked_out(symbol, now)
        if locked:
            logger.info(
                "MacroGatekeeper: VETOED by temporal lockout '%s' "
                "for %s at %s EDT.",
                lockout_name, symbol, now.strftime("%H:%M:%S"),
            )
            result = MacroDecisionPayload(
                macro_regime="LOCKOUT",
                market_bias="NEUTRAL",
                confidence_score=1.0,
                risk_multiplier=0.0,
                trade_permitted=False,
                inhibition_reason=(
                    f"Temporal lockout active: {lockout_name} "
                    f"({now.strftime('%H:%M:%S')} EDT)."
                ),
                high_impact_warning=lockout_name,
            )
            self._log_decision(signal_context, headline, result, source="LOCKOUT", now=now)
            return result

        # --- Layer 2a: Semantic Cache ---
        cache_key = headline or self._build_cache_key(signal_context)
        cached    = self._cache.lookup(cache_key)
        if cached:
            try:
                result = MacroDecisionPayload(**cached)
                self._log_decision(signal_context, headline, result, source="CACHE", now=now)
                return result
            except Exception as exc:
                logger.warning(
                    "MacroGatekeeper: cache payload parse failed (%s) — "
                    "proceeding to LLM.", exc,
                )

        # --- Layer 2b: LLM Token Bucket ---
        acquired = self._llm_bucket.acquire(timeout=5.0)
        if not acquired:
            logger.warning(
                "MacroGatekeeper: LLM token bucket timed out — degradation."
            )
            result = self._degraded_payload("LLM rate-limiter bucket timed out.")
            self._log_decision(signal_context, headline, result, source="DEGRADED", now=now)
            return result

        # --- Layer 2c: Gemini API call with timeout ---
        prompt = self._build_prompt(signal_context, headline, now)

        try:
            future = self._executor.submit(self._call_gemini_with_retry, prompt)
            payload = future.result(timeout=self._timeout_sec)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "MacroGatekeeper: Gemini call exceeded %.0fms timeout — degradation.",
                self._timeout_sec * 1000,
            )
            result = self._degraded_payload(
                f"Gemini API call timed out (>{self._timeout_sec*1000:.0f}ms)."
            )
            self._log_decision(signal_context, headline, result, source="DEGRADED_TIMEOUT", now=now)
            return result
        except Exception as exc:
            logger.exception(
                "MacroGatekeeper: unexpected error calling Gemini: %s — degradation.",
                exc,
            )
            result = self._degraded_payload(f"Gemini call failed: {exc}")
            self._log_decision(signal_context, headline, result, source="DEGRADED_ERROR", now=now)
            return result

        # --- Store in cache ---
        if payload is not None:
            self._cache.store(cache_key, payload.model_dump())
            self._log_decision(signal_context, headline, payload, source="LLM", now=now)
            return payload

        result = self._degraded_payload("Gemini returned empty payload.")
        self._log_decision(signal_context, headline, result, source="DEGRADED_EMPTY", now=now)
        return result

    # ------------------------------------------------------------------
    # Temporal lockout check
    # ------------------------------------------------------------------

    def is_locked_out(
        self,
        symbol: str,
        now:    Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """
        Check all configured temporal lockout windows.

        Args:
            symbol: Uppercase ticker symbol.
            now:    Current EDT datetime. Defaults to datetime.now(EDT).

        Returns:
            (True,  lockout_name) if a lockout is currently active.
            (False, "")           if trading is permitted on time basis.
        """
        if now is None:
            now = datetime.now(_EDT)

        for lockout in self._lockouts:
            if lockout.is_active(symbol, now):
                return True, lockout.name

        return False, ""

    # ------------------------------------------------------------------
    # Gemini API call with 429 exponential backoff
    # ------------------------------------------------------------------

    def _call_gemini_with_retry(self, prompt: str) -> Optional[MacroDecisionPayload]:
        """
        Call Gemini 2.5 Flash with exponential backoff on HTTP 429.

        This method runs in a ThreadPoolExecutor thread and is subject to
        the outer 1500ms wall-clock timeout enforced by the caller.

        Retry policy (from spec): exponential backoff on HTTP 429.
            Attempt 1: immediate
            Attempt 2: 1s backoff
            Attempt 3: 2s backoff  (max attempts: _MAX_RETRIES_429)
        """
        client  = self._get_gemini_client()
        backoff = _BACKOFF_INITIAL_SEC

        for attempt in range(1, _MAX_RETRIES_429 + 1):
            try:
                return self._call_gemini_once(client, prompt)

            except Exception as exc:
                exc_str = str(exc)

                # Detect HTTP 429 (rate limit) from the exception message
                if "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str:
                    if attempt >= _MAX_RETRIES_429:
                        logger.error(
                            "MacroGatekeeper: Gemini 429 persists after %d retries.",
                            _MAX_RETRIES_429,
                        )
                        return None   # Caller applies degradation
                    logger.warning(
                        "MacroGatekeeper: Gemini 429 (attempt %d/%d) — "
                        "backoff %.1fs.",
                        attempt, _MAX_RETRIES_429, backoff,
                    )
                    time.sleep(min(backoff, _BACKOFF_MAX_SEC))
                    backoff *= 2
                else:
                    logger.error(
                        "MacroGatekeeper: Gemini API error (attempt %d): %s",
                        attempt, exc,
                    )
                    return None

        return None

    def _call_gemini_once(self, client, prompt: str) -> MacroDecisionPayload:
        """
        Single Gemini API call using google-genai structured output.

        The API is instructed to return a JSON object matching the
        MacroDecisionPayload Pydantic schema via response_mime_type and
        response_schema configuration.
        """
        from google.genai import types as genai_types

        config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=MacroDecisionPayload,
        )

        response = client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=config,
        )

        # Parse the JSON response text into the Pydantic model
        raw_json = response.text
        payload  = MacroDecisionPayload.model_validate_json(raw_json)

        logger.info(
            "MacroGatekeeper: Gemini response — regime=%s bias=%s "
            "permitted=%s risk_mult=%.2f",
            payload.macro_regime, payload.market_bias,
            payload.trade_permitted, payload.risk_multiplier,
        )
        return payload

    # ------------------------------------------------------------------
    # Gemini client initialisation (lazy, thread-safe)
    # ------------------------------------------------------------------

    def _get_gemini_client(self):
        """
        Lazily initialise and cache the google-genai client.
        Uses the GEMINI_API_KEY environment variable.
        Thread-safe via double-checked locking.
        """
        if self._gemini_client is not None:
            return self._gemini_client

        with self._gemini_lock:
            if self._gemini_client is not None:
                return self._gemini_client

            import google.genai as genai

            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                logger.warning(
                    "MacroGatekeeper: GEMINI_API_KEY not set — "
                    "LLM calls will fail and degrade gracefully."
                )

            self._gemini_client = genai.Client(api_key=api_key)
            logger.info(
                "MacroGatekeeper: Gemini client initialised (model=%s).",
                self._model,
            )
            return self._gemini_client

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        signal_context: Dict[str, Any],
        headline:       Optional[str],
        now:            datetime,
    ) -> str:
        """
        Construct the Gemini evaluation prompt.

        The prompt provides the signal parameters (symbol, strategy, regime,
        entry/stop/target prices) and any available news headline, then
        instructs the model to return a structured MacroDecisionPayload.
        """
        symbol   = signal_context.get("symbol",   "UNKNOWN")
        strategy = signal_context.get("strategy", "UNKNOWN")
        regime   = signal_context.get("regime",   "UNKNOWN")
        entry    = signal_context.get("entry_price",  "N/A")
        stop     = signal_context.get("stop_price",   "N/A")
        target   = signal_context.get("target_price", "N/A")
        qty      = signal_context.get("quantity",     "N/A")

        headline_line = (
            f"Current market headline / context:\n  \"{headline}\"\n\n"
            if headline
            else "No specific news headline provided.\n\n"
        )

        return (
            f"You are a quantitative macroeconomic risk analyst for an automated "
            f"day-trading engine. Evaluate whether the following trade signal "
            f"should be PERMITTED or VETOED based on current macroeconomic "
            f"conditions and market microstructure.\n\n"
            f"Signal Details:\n"
            f"  Symbol:    {symbol}\n"
            f"  Strategy:  {strategy}\n"
            f"  Regime:    {regime}\n"
            f"  Entry:     ${entry}\n"
            f"  Stop:      ${stop}\n"
            f"  Target:    ${target}\n"
            f"  Quantity:  {qty} shares\n"
            f"  Time (EDT): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
            f"{headline_line}"
            f"Assess the macro environment. If there is a hawkish surprise, "
            f"a risk-off shock, an active geopolitical event, or any factor "
            f"that would significantly increase the probability of a stop-loss "
            f"hit on a leveraged ETF position, set trade_permitted=false and "
            f"explain in inhibition_reason.\n\n"
            f"Return a JSON object matching the MacroDecisionPayload schema:\n"
            f"  macro_regime: string (e.g. RISK_OFF, NEUTRAL, RISK_ON)\n"
            f"  market_bias: string (BULLISH, BEARISH, or NEUTRAL)\n"
            f"  confidence_score: float [0.0-1.0]\n"
            f"  risk_multiplier: float [0.0-1.0] (1.0=full size, 0.0=no trade)\n"
            f"  trade_permitted: boolean\n"
            f"  inhibition_reason: string or null\n"
            f"  high_impact_warning: string or null\n"
        )

    # ------------------------------------------------------------------
    # Cache key builder
    # ------------------------------------------------------------------

    def _build_cache_key(self, signal_context: Dict[str, Any]) -> str:
        """
        Build a cache lookup key from signal context when no headline is available.
        Uses symbol + strategy + regime as a generic context string.
        """
        symbol   = signal_context.get("symbol",   "")
        strategy = signal_context.get("strategy", "")
        regime   = signal_context.get("regime",   "")
        return f"{symbol} {strategy} trade signal — regime: {regime}"

    # ------------------------------------------------------------------
    # Degradation policy
    # ------------------------------------------------------------------

    def _degraded_payload(self, reason: str) -> MacroDecisionPayload:
        """
        Return the spec-defined degradation payload.

        Spec: On timeout or HTTP 429 → trade_permitted=True, risk_multiplier=0.50.
        This allows the trade to proceed but at half normal position size,
        prioritising execution continuity over complete risk avoidance.
        """
        logger.warning(
            "MacroGatekeeper: DEGRADED MODE — %s | "
            "trade_permitted=True, risk_multiplier=%.2f",
            reason, _DEGRADED_RISK_MULTIPLIER,
        )
        return MacroDecisionPayload(
            macro_regime="DEGRADED",
            market_bias="NEUTRAL",
            confidence_score=0.0,
            risk_multiplier=_DEGRADED_RISK_MULTIPLIER,
            trade_permitted=_DEGRADED_TRADE_PERMITTED,
            inhibition_reason=reason,
            high_impact_warning=None,
        )

    # ------------------------------------------------------------------
    # Decision logger (for future model distillation)
    # ------------------------------------------------------------------

    def _log_decision(
        self,
        signal_context: Dict[str, Any],
        headline:       Optional[str],
        payload:        MacroDecisionPayload,
        source:         str,
        now:            datetime,
    ) -> None:
        """
        Append a macro decision record to the JSONL distillation log.

        Each record contains the full input context and output decision,
        enabling offline analysis to determine whether the LLM is adding
        value vs. the temporal lockouts alone, and to fine-tune a lighter
        model (e.g. Flash) using Pro's decisions as training labels.

        Args:
            signal_context: The trade signal context dict passed to evaluate().
            headline:       Optional news headline string.
            payload:        The MacroDecisionPayload that was returned.
            source:         Where the decision came from: LLM, CACHE, LOCKOUT, DEGRADED_*.
            now:            The evaluation timestamp (EDT).
        """
        record = {
            "ts":             now.isoformat(),
            "source":         source,
            "model":          self._model if source == "LLM" else None,
            "signal_context": signal_context,
            "headline":       headline,
            "decision": {
                "macro_regime":        payload.macro_regime,
                "market_bias":         payload.market_bias,
                "confidence_score":    payload.confidence_score,
                "risk_multiplier":     payload.risk_multiplier,
                "trade_permitted":     payload.trade_permitted,
                "inhibition_reason":   payload.inhibition_reason,
                "high_impact_warning": payload.high_impact_warning,
            },
        }
        try:
            with self._decision_log_lock:
                with open(self._decision_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning("MacroGatekeeper: decision log write failed: %s", exc)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Cleanly shut down the thread pool executor."""
        self._executor.shutdown(wait=False)
        logger.info("MacroGatekeeper: thread pool shut down.")


# ===========================================================================
# Factory
# ===========================================================================

def build_from_config(cfg: dict) -> Tuple["MacroGatekeeper", MacroSemanticCache]:
    """
    Construct and wire both Phase 5 components from the loaded config.yaml dict.

    Args:
        cfg: Top-level config dict.

    Returns:
        (gatekeeper, cache) — both fully initialised.
    """
    from macro.semantic_cache import build_from_config as build_cache
    from typing import Tuple as _Tuple

    cache      = build_cache(cfg)
    gatekeeper = MacroGatekeeper(cfg=cfg, cache=cache)
    return gatekeeper, cache
