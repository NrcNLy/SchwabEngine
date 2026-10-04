"""
governor.py
===========
Tier 2 Background Strategy Governor for the Schwab Day-Trading Engine.
Responsible for macro ingestion, regime detection, and post-market reflection
using Google Cloud Vertex AI (Gemini 2.5 Flash / Pro).

Executes asynchronously from the deterministic Tier 1 execution engine.
Communicates via atomic state serialization to `strategy_config.json`.
"""

import os
import sys
import json
import time
import asyncio
import argparse
import logging
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("google-genai SDK not found. Install via: pip install google-genai")
    sys.exit(1)

from core.models import GovernorConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("governor")

PROJECT_ID = "gen-lang-client-0334702303"
LOCATION = "us-central1"

# Firewall safeguard against Partner models
ALLOWED_MODELS = {"gemini-2.5-flash", "gemini-2.5-pro"}

class GovernorDaemon:
    def __init__(self):
        self.client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
        self.config_path = Path("strategy_config.json")
        
    def _verify_model_safeguard(self, model: str):
        if model not in ALLOWED_MODELS:
            raise ValueError(f"CRITICAL BILLING SAFEGUARD: Model '{model}' is explicitly blocked. "
                             f"Only allowed models: {ALLOWED_MODELS}")

    async def update_strategy_config(self, updates: dict):
        """Atomic file write to prevent locking out the Tier 1 engine."""
        config = {}
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text())
            except Exception:
                pass
        
        config.update(updates)
        config["_updated_at"] = datetime.utcnow().isoformat() + "Z"
        
        json_payload = json.dumps(config, indent=2)
        
        # Write to a temporary file on the same filesystem
        temp_fd, temp_path = tempfile.mkstemp(dir="./")
        with os.fdopen(temp_fd, 'w') as f:
            f.write(json_payload)
            f.flush()
            os.fsync(f.fileno()) # Forces OS to sync data to the physical disk platter
        
        # Guarantees absolute data integrity via OS-level atomic file replacement
        os.replace(temp_path, str(self.config_path))
        logger.info(f"Strategy configuration atomically updated: {self.config_path}")

    async def pre_market_macro(self):
        """08:35 EDT Routine: Fast pre-market indexing and regime setting using Structured Outputs."""
        logger.info("Executing Pre-Market Macro Routine (08:35 EDT)")
        prompt = (
            "Analyze the pre-market conditions for QQQ and SPY. "
            "Determine the target intraday regime (Regime A or Regime C) "
            "for leveraged ETFs: SOXL, TQQQ, TNA. Return a strict JSON configuration."
        )
        self._verify_model_safeguard("gemini-2.5-flash")
        try:
            logger.info("Dispatching prompt to Vertex AI (gemini-2.5-flash) asynchronously...")
            response = await self.client.aio.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GovernorConfig,
                    temperature=0.2
                )
            )
            logger.info("Vertex AI Structured Response received.")
            
            # The SDK parses the JSON response and instantiates the Pydantic class
            # Ensure we serialize it to dict safely
            try:
                # Based on the genai SDK, it might return a Pydantic object directly if we parse it,
                # or a text string containing valid JSON. We parse it to be safe.
                response_data = json.loads(response.text)
                await self.update_strategy_config(response_data)
                logger.info("Pre-Market Routine Completed Successfully.")
            except Exception as parse_exc:
                 logger.error(f"Failed to parse or apply structured output: {parse_exc}")

        except Exception as exc:
            logger.error(f"Macro routine failed: {exc}")

    async def run_smoke_test(self):
        """Verifies Vertex AI API client connectivity under the required GCP project."""
        logger.info("Initiating dry-run smoke test against Vertex AI...")
        prompt = "Return exactly the word 'ACKNOWLEDGED'."
        try:
            res = await self.client.aio.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            logger.info(f"Smoke test successful. Vertex AI Response: {res.text.strip()}")
        except Exception as exc:
            logger.error(f"Smoke test failed! Connection or authentication error: {exc}")
            sys.exit(1)

    async def parse_document(self, doc_path: Path):
        """Dispatches financial document parsing to DocumentParser."""
        from services.document_parser import DocumentParser
        logger.info(f"Governor triggering multimodal extraction for: {doc_path}")
        parser = DocumentParser()
        snapshot, is_dup = await parser.extract_credit_report(doc_path)
        logger.info(f"Extraction completed. Bureau: {snapshot.bureau}, Discrepancies: {len(snapshot.detected_discrepancies)}, IsDuplicate: {is_dup}")
        return snapshot

def main():
    parser = argparse.ArgumentParser(description="Schwab Tier 2 Background Governor")
    parser.add_argument("--macro", action="store_true", help="Run pre-market macro ingestion")
    parser.add_argument("--reflect", action="store_true", help="Run post-market reflection")
    parser.add_argument("--test", action="store_true", help="Run dry-run Vertex AI smoke test")
    parser.add_argument("--parse-doc", type=str, help="Path to credit or financial document to parse")
    args = parser.parse_args()

    daemon = GovernorDaemon()

    if args.test:
        asyncio.run(daemon.run_smoke_test())
    elif args.macro:
        asyncio.run(daemon.pre_market_macro())
    elif args.parse_doc:
        asyncio.run(daemon.parse_document(Path(args.parse_doc)))
    elif args.reflect:
        logger.warning("Reflection not explicitly typed yet.")
    else:
        logger.info("Governor Daemon initialized. Use --macro, --reflect, --test, or --parse-doc flags to execute sequences.")

if __name__ == "__main__":
    main()
