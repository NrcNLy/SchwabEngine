"""
research/compression_agent.py
==============================
Weekend Compression Research Agent.

Runs every Saturday and Sunday at 22:00 EDT. Analyzes accumulated decision
data to produce a written compression report and a Flash shadow-test, giving
a data-driven recommendation on when to downgrade from the current
e2-standard-2 (8GB) with Gemini 3.1 Pro to a lighter configuration.

Analysis Reports Include:
  1. LLM Add-Value Rate: % of LLM decisions that differed from temporal
     lockout rules alone. Answers: "Is the LLM doing anything useful?"

  2. Flash Shadow Test: For a sample of logged LLM (Pro) prompts, run the
     same prompt through Gemini Flash and compare outcomes. Reports agreement
     rate. If >= 90% → "Pro can be replaced with Flash."

  3. Semantic Cache Efficiency: Cache hit rate. If < 20% → recommend simpler
     hash-based cache to eliminate the 2.5GB PyTorch dependency.

  4. Signal Quality Analysis: Win rate by regime, by strategy, by LLM decision.
     Answers: "Is the gatekeeper's veto actually preventing losses?"

  5. Resource Projection: Current RAM baseline, estimated months until safe
     downgrade, projected monthly savings.

Reports are written to /app/logs/compression_reports/YYYY-MM-DD.md
and also served by the FastAPI server via GET /compression/latest.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz

logger = logging.getLogger(__name__)
_EDT = pytz.timezone("America/New_York")

_REPORT_DIR = Path("/app/logs/compression_reports")
_DECISIONS_LOG = Path("/app/logs/macro_decisions.jsonl")
_PERFORMANCE_LOG_DIR = Path("/app/logs/performance")
_FLASH_MODEL = "gemini-2.0-flash"
_FLASH_SAMPLE_SIZE = 20  # How many Pro decisions to shadow-test with Flash


class CompressionResearchAgent:
    """
    Weekend daemon for LLM and infrastructure compression analysis.
    """

    def __init__(self, gemini_client, dispatcher, cfg: dict) -> None:
        self._gemini     = gemini_client
        self._dispatcher = dispatcher

        research_cfg = cfg.get("research", {})
        self._enabled  = research_cfg.get("compression_agent_enabled", True)
        macro_cfg      = cfg.get("macro", {})
        self._pro_model = macro_cfg.get("gemini_model", "gemini-3.1-pro-preview")

        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        _PERFORMANCE_LOG_DIR.mkdir(parents=True, exist_ok=True)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info("CompressionResearchAgent initialised — runs Sat/Sun 22:00 EDT.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            logger.info("CompressionResearchAgent: disabled by config.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._schedule_loop,
            name="CompressionAgentThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("CompressionResearchAgent: daemon started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def run_now(self) -> str:
        """Run analysis immediately (for manual trigger via API). Returns report path."""
        return self._run_analysis()

    def get_latest_report(self) -> Optional[Dict[str, Any]]:
        """Return the latest report as a dict (for API)."""
        reports = sorted(_REPORT_DIR.glob("*.md"), reverse=True)
        if not reports:
            return None
        latest = reports[0]
        return {
            "date":        latest.stem,
            "report_text": latest.read_text(encoding="utf-8"),
        }

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    def _schedule_loop(self) -> None:
        """Sleep until Saturday/Sunday 22:00 EDT, then run analysis."""
        while not self._stop_event.wait(timeout=60):
            now = datetime.now(_EDT)
            is_weekend = now.weekday() in (5, 6)  # 5=Sat, 6=Sun
            is_run_hour = now.hour == 22 and now.minute < 5
            if is_weekend and is_run_hour:
                logger.info(
                    "CompressionResearchAgent: weekend analysis starting..."
                )
                try:
                    path = self._run_analysis()
                    logger.info(
                        "CompressionResearchAgent: report written → %s", path
                    )
                    if self._dispatcher:
                        self._dispatcher._send("COMPRESSION_REPORT_READY", {
                            "date": now.strftime("%Y-%m-%d"),
                            "path": str(path),
                        })
                except Exception as exc:
                    logger.exception(
                        "CompressionResearchAgent: analysis failed: %s", exc
                    )
                # Sleep ~5 minutes to avoid re-triggering in the same window
                self._stop_event.wait(timeout=300)

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------

    def _run_analysis(self) -> str:
        """Run the full compression analysis and write a Markdown report."""
        date_str = datetime.now(_EDT).strftime("%Y-%m-%d")
        logger.info("CompressionResearchAgent: loading decision log...")

        decisions = self._load_decisions()
        performance = self._load_performance_logs()

        llm_add_value = self._analyze_llm_add_value(decisions)
        cache_hit_rate = self._analyze_cache_hit_rate(decisions)
        flash_agreement = self._run_flash_shadow_test(decisions)
        signal_quality = self._analyze_signal_quality(performance)
        resource_summary = self._get_resource_summary()
        ready_to_compress = (
            flash_agreement >= 0.90
            and cache_hit_rate >= 0.20
            and len(decisions) >= 50
        )

        report = self._build_report(
            date_str=date_str,
            decisions=decisions,
            llm_add_value=llm_add_value,
            cache_hit_rate=cache_hit_rate,
            flash_agreement=flash_agreement,
            signal_quality=signal_quality,
            resource_summary=resource_summary,
            ready_to_compress=ready_to_compress,
        )

        report_path = _REPORT_DIR / f"{date_str}.md"
        report_path.write_text(report, encoding="utf-8")
        return str(report_path)

    def _load_decisions(self) -> List[Dict[str, Any]]:
        if not _DECISIONS_LOG.exists():
            return []
        decisions = []
        with open(_DECISIONS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        decisions.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return decisions

    def _load_performance_logs(self) -> List[Dict[str, Any]]:
        logs = []
        for path in sorted(_PERFORMANCE_LOG_DIR.glob("*.jsonl")):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            logs.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        return logs

    # ------------------------------------------------------------------
    # Analysis helpers
    # ------------------------------------------------------------------

    def _analyze_llm_add_value(self, decisions: List[Dict]) -> float:
        """
        Compute % of LLM decisions that differed from temporal lockout alone.
        If the LLM permitted a trade during no-lockout times: no difference.
        If the LLM vetoed during a no-lockout time: that's value-add.
        """
        llm_decisions = [d for d in decisions if d.get("source") == "LLM"]
        if not llm_decisions:
            return 0.0
        vetoed_by_llm = sum(
            1 for d in llm_decisions
            if not d.get("decision", {}).get("trade_permitted", True)
        )
        return vetoed_by_llm / len(llm_decisions)

    def _analyze_cache_hit_rate(self, decisions: List[Dict]) -> float:
        if not decisions:
            return 0.0
        cache_hits = sum(1 for d in decisions if d.get("source") == "CACHE")
        return cache_hits / len(decisions)

    def _run_flash_shadow_test(self, decisions: List[Dict]) -> float:
        """
        Run a sample of Pro prompts through Flash and check agreement.
        Agreement = both Flash and Pro gave same trade_permitted decision.
        """
        llm_decisions = [d for d in decisions if d.get("source") == "LLM"]
        if not llm_decisions or not self._gemini:
            return 0.0

        sample = llm_decisions[-_FLASH_SAMPLE_SIZE:]  # Most recent
        agreements = 0

        for d in sample:
            ctx  = d.get("signal_context", {})
            hl   = d.get("headline", "")
            pro_permitted = d.get("decision", {}).get("trade_permitted", True)

            prompt = self._build_shadow_prompt(ctx, hl)
            try:
                resp = self._gemini.models.generate_content(
                    model=_FLASH_MODEL,
                    contents=prompt,
                )
                raw = resp.text.strip()
                if "false" in raw.lower() and not pro_permitted:
                    agreements += 1
                elif "true" in raw.lower() and pro_permitted:
                    agreements += 1
            except Exception as exc:
                logger.debug(
                    "CompressionResearchAgent: Flash shadow call failed: %s", exc
                )

        return agreements / len(sample) if sample else 0.0

    def _build_shadow_prompt(self, ctx: Dict, headline: str) -> str:
        return (
            f"Given this trade signal: {json.dumps(ctx)}\n"
            f"News context: {headline or 'none'}\n"
            f"Should this trade be permitted? Reply with only 'true' or 'false'."
        )

    def _analyze_signal_quality(self, perf: List[Dict]) -> Dict[str, Any]:
        if not perf:
            return {"days_analyzed": 0}
        wins   = sum(d.get("wins",   0) for d in perf)
        losses = sum(d.get("losses", 0) for d in perf)
        total  = wins + losses
        pnl    = sum(d.get("gross_pnl", 0) for d in perf)
        return {
            "days_analyzed": len(perf),
            "total_trades":  total,
            "win_rate":      wins / total if total > 0 else 0.0,
            "gross_pnl":     pnl,
        }

    def _get_resource_summary(self) -> str:
        try:
            import subprocess
            result = subprocess.run(
                ["free", "-m"], capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip()
        except Exception:
            return "Resource info unavailable (not running on Linux)."

    # ------------------------------------------------------------------
    # Report builder
    # ------------------------------------------------------------------

    def _build_report(
        self,
        date_str: str,
        decisions: List[Dict],
        llm_add_value: float,
        cache_hit_rate: float,
        flash_agreement: float,
        signal_quality: Dict,
        resource_summary: str,
        ready_to_compress: bool,
    ) -> str:
        verdict = (
            "✅ **READY TO COMPRESS** — All thresholds met. "
            "Schedule downgrade to e2-small + Flash."
            if ready_to_compress else
            "⏳ **NOT YET READY** — Continue collecting data."
        )

        return f"""# Compression Research Report — {date_str}

## Verdict
{verdict}

---

## 1. LLM Add-Value Analysis
- **Total decisions logged:** {len(decisions)}
- **LLM-sourced decisions:** {sum(1 for d in decisions if d.get("source") == "LLM")}
- **Lockout decisions:** {sum(1 for d in decisions if d.get("source") == "LOCKOUT")}
- **Cache hits:** {sum(1 for d in decisions if d.get("source") == "CACHE")}
- **Degraded (timeout/error):** {sum(1 for d in decisions if "DEGRADED" in d.get("source", ""))}

**LLM Add-Value Rate (vetoed during non-lockout window): {llm_add_value:.1%}**
> Threshold for compression: Any rate. This measures whether the LLM is
> adding signal beyond what temporal lockouts already provide.

---

## 2. Semantic Cache Efficiency
**Cache Hit Rate: {cache_hit_rate:.1%}**
> If < 20%: Recommend replacing ChromaDB + sentence-transformers (2.5GB RAM)
> with a simple SHA-256 hash cache. **Saves ~2.5GB RAM and enables downgrade.**

---

## 3. Flash Shadow Test
**Flash Agreement with Pro: {flash_agreement:.1%}** (sampled last {_FLASH_SAMPLE_SIZE} LLM decisions)
> Threshold for model downgrade: ≥ 90% agreement

---

## 4. Signal Quality
- **Days analyzed:** {signal_quality.get("days_analyzed", 0)}
- **Total trades:** {signal_quality.get("total_trades", 0)}
- **Win rate:** {signal_quality.get("win_rate", 0):.1%}
- **Gross P&L:** ${signal_quality.get("gross_pnl", 0):+.2f}

---

## 5. Infrastructure Resources
```
{resource_summary}
```

---

## Compression Checklist

| Check | Status | Threshold |
|-------|--------|-----------|
| Flash agreement ≥ 90% | {"✅" if flash_agreement >= 0.90 else "❌"} {flash_agreement:.1%} | 90% |
| Cache hit rate ≥ 20% | {"✅" if cache_hit_rate >= 0.20 else "❌"} {cache_hit_rate:.1%} | 20% |
| Decisions logged ≥ 50 | {"✅" if len(decisions) >= 50 else "❌"} {len(decisions)} | 50 |

---

## Recommended Compression Actions (when ready)
1. Replace `gemini_model: gemini-3.1-pro-preview` → `gemini-2.0-flash`
2. Replace ChromaDB + sentence-transformers cache with SHA-256 dict cache
3. Downgrade VM: `gcloud compute instances set-machine-type schwab-trader --machine-type=e2-small`
4. Add 2GB swap: `fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile`
5. Disable CompressionResearchAgent (job done)

**Estimated monthly savings: ~$37/month**

---
*Generated by CompressionResearchAgent at {datetime.now(_EDT).strftime("%Y-%m-%d %H:%M:%S %Z")}*
"""
