"""
services/document_parser.py
===========================
Tier 2 Multimodal Financial Document Extraction Daemon.
Uses Google Cloud Vertex AI (Gemini 2.5 Pro via google-genai SDK)
to auto-classify and extract UnifiedDocumentSnapshot records.
"""

import os
import asyncio
import json
import time
import hashlib
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional, Tuple

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from core.liquidity_models import (
    DocumentClass,
    DocumentLineItem,
    UnifiedDocumentSnapshot,
    PromotionalDebt,
    CollateralInvariantState,
    LiquidityTarget,
    extract_snapshot,
)
from core.collateral_engine import CollateralEngine
from core.atomic_io import atomic_write_json, read_json, update_json
from core.paths import PROCESSED_HASHES_FILE, STATE_DIR

logger = logging.getLogger("document_parser")

PROJECT_ID = "gen-lang-client-0334702303"
LOCATION = "us-central1"
ALLOWED_MODELS = {"gemini-2.5-pro", "gemini-2.5-flash"}


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


class DocumentExtractionError(RuntimeError):
    """Raised when a document cannot be extracted and no fabricated fallback is permitted."""


class DocumentParser:
    def __init__(
        self,
        output_dir: Optional[Path] = None,
        cash_provider: Optional[Callable[[], Tuple[Decimal, Decimal]]] = None,
    ):
        """
        cash_provider: returns (settled_cash, unsettled_cash) from the live ledger, used only to
        evaluate the Net Collateral Buffer. Without one, balances default to zero.
        """
        self.output_dir = output_dir or STATE_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.output_dir / "macro_liquidity.json"
        self.hashes_file = PROCESSED_HASHES_FILE
        self.hashes_file.parent.mkdir(parents=True, exist_ok=True)
        self.cash_provider = cash_provider
        # The deterministic sample parser returns invented tradelines. It is only ever
        # used when explicitly enabled (local development); never silently.
        self.allow_sample_fallback = os.environ.get("DOC_PARSER_SAMPLE_FALLBACK", "") == "1"

        self.client = None
        if genai:
            try:
                # Configure google-genai client with 120s timeout (HttpOptions.timeout is in milliseconds)
                self.client = genai.Client(
                    vertexai=True,
                    project=PROJECT_ID,
                    location=LOCATION,
                    http_options={"api_version": "v1", "timeout": 120_000}
                )
            except Exception as e:
                logger.warning(f"Could not initialize Vertex AI genai Client: {e}.")

    def compute_sha256(self, file_bytes: bytes) -> str:
        """Computes the SHA-256 hash of file bytes for duplicate detection."""
        return hashlib.sha256(file_bytes).hexdigest()

    def is_duplicate(self, file_hash: str) -> bool:
        """Checks if file hash has already been processed."""
        hashes = read_json(self.hashes_file, default={})
        return isinstance(hashes, dict) and file_hash in hashes

    def record_processed_hash(self, file_hash: str, filename: str) -> None:
        """Records processed hash to prevent duplicate AI extraction cost."""
        def mutate(doc: dict) -> None:
            doc[file_hash] = {
                "filename": filename,
                "processed_at": datetime.utcnow().isoformat() + "Z",
            }
        try:
            update_json(self.hashes_file, mutate, default={})
        except Exception as e:
            logger.error(f"Failed to record processed hash: {e}")

    def _atomic_write_state(self, state_data: dict, max_retries: int = 5, retry_delay: float = 0.15) -> None:
        """
        Atomically saves state data to state/macro_liquidity.json (temp file + fsync + os.replace,
        retrying on Windows WinError 32 file locks) via core.atomic_io.
        """
        atomic_write_json(self.state_file, state_data, max_retries=max_retries, retry_delay=retry_delay)
        logger.info(f"State atomically updated to {self.state_file}")

    def parse_with_fallback(self, file_bytes: bytes, filename: str) -> UnifiedDocumentSnapshot:
        """
        Deterministic fallback parser used in local environments.
        """
        logger.info(f"Using deterministic fallback extraction for '{filename}'")
        today = date.today()
        line_items = [
            DocumentLineItem(
                account_name="CITI SIMPLICITY CARD",
                masked_account_number="************4321",
                credit_limit=Decimal("12000.00"),
                current_balance=Decimal("2840.00"),
                monthly_payment=Decimal("60.00"),
                date_opened=date(2022, 5, 14),
                last_reported=today,
                is_promotional=False
            )
        ]
        
        return UnifiedDocumentSnapshot(
            document_class=DocumentClass.CREDIT_REPORT,
            institution_or_bureau="EXPERIAN",
            report_date=today,
            total_revolving_limit=Decimal("12000.00"),
            total_revolving_balance=Decimal("2840.00"),
            aggregate_utilization_pct=23.6,
            hard_inquiries_count=2,
            line_items=line_items
        )

    async def extract_document(
        self, 
        file_path: Path,
        force: bool = False
    ) -> Tuple[UnifiedDocumentSnapshot, bool]:
        """
        Main extraction entrypoint:
        1. Checks SHA-256 for duplicates.
        2. Sends file bytes to Vertex AI gemini-2.5-pro for auto-classification.
        3. Atomically persists macro liquidity state.
        Returns: (snapshot, is_duplicate)
        """
        file_bytes = file_path.read_bytes()
        file_hash = self.compute_sha256(file_bytes)

        if not force and self.is_duplicate(file_hash):
            logger.info(f"Duplicate file detected (SHA-256: {file_hash[:12]}...). Skipping re-extraction.")
            stored = read_json(self.state_file, default=None)
            if isinstance(stored, dict):
                try:
                    existing = extract_snapshot(stored)
                except Exception as exc:
                    logger.warning(f"Stored snapshot unreadable ({exc}); re-extracting.")
                    existing = None
                if existing is not None:
                    return existing, True

        snapshot = None
        extraction_error: Optional[Exception] = None
        if self.client:
            prompt = (
                "You are an institutional financial document analysis system. "
                "Analyze the attached document. First, zero-shot classify it into one of the DocumentClass enum values "
                "(CREDIT_REPORT, BANK_STATEMENT, CREDIT_CARD_STATEMENT, PAYSTUB, STUDENT_LOAN_STATEMENT, TAX_DOCUMENT, MISCELLANEOUS_FINANCIAL). "
                "Extract the primary institution, document date, and populate line items. "
                "For credit/bank statements, extract limits, balances, and payments. "
                "For paystubs or tax docs, use notes/balances appropriately. "
                "Fail gracefully if data is missing, but return valid JSON matching UnifiedDocumentSnapshot."
            )
            try:
                logger.info(f"Dispatching '{file_path.name}' to Vertex AI gemini-2.5-pro for auto-classification...")
                ext = file_path.suffix.lower()
                mime = "application/pdf" if ext == ".pdf" else "image/png" if ext == ".png" else "image/jpeg"
                
                response = await self.client.aio.models.generate_content(
                    model='gemini-2.5-pro',
                    contents=[
                        types.Part.from_bytes(data=file_bytes, mime_type=mime),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=UnifiedDocumentSnapshot,
                        temperature=0.1
                    )
                )
                logger.info("Vertex AI document response received.")
                parsed_json = json.loads(response.text)
                snapshot = UnifiedDocumentSnapshot(**parsed_json)
            except Exception as e:
                logger.error(f"Vertex AI extraction failed: {e}")
                extraction_error = e

        if snapshot is None:
            if not self.allow_sample_fallback:
                reason = extraction_error or "Vertex AI client is not configured"
                raise DocumentExtractionError(f"Could not extract '{file_path.name}': {reason}")
            logger.warning("DOC_PARSER_SAMPLE_FALLBACK=1: persisting SAMPLE data (not from the document).")
            snapshot = self.parse_with_fallback(file_bytes, file_path.name)

        settled_cash, unsettled_cash = Decimal("0"), Decimal("0")
        if self.cash_provider is not None:
            try:
                settled_cash, unsettled_cash = self.cash_provider()
            except Exception as e:
                logger.warning(f"Cash provider failed ({e}); evaluating collateral buffer with zero cash.")

        def merge_and_persist() -> None:
            self.record_processed_hash(file_hash, file_path.name)

            def mutate(curr: dict) -> None:
                # Preserve manual promotional debts, backstops, and liquidity targets
                collateral_engine = CollateralEngine()
                liquidity_targets = []
                try:
                    for d_dict in curr.get("promotional_debts", []):
                        collateral_engine.add_or_update_promotional_debt(PromotionalDebt(**d_dict))
                    if "external_liquid_backstop" in curr:
                        collateral_engine.external_liquid_backstop = Decimal(str(curr["external_liquid_backstop"]))
                    liquidity_targets = [LiquidityTarget(**lt) for lt in curr.get("liquidity_targets", [])]
                except Exception as e:
                    logger.warning(f"Failed to read existing state for merging: {e}")

                collateral_engine.merge_document_snapshot(snapshot)
                state = collateral_engine.evaluate_invariant(
                    settled_cash=Decimal(str(settled_cash)),
                    unsettled_cash=Decimal(str(unsettled_cash)),
                )
                curr.update({
                    "_updated_at": datetime.utcnow().isoformat() + "Z",
                    "file_source": file_path.name,
                    "sha256": file_hash,
                    "snapshot": snapshot.model_dump(),
                    "collateral_state": state.model_dump(),
                    "promotional_debts": [d.model_dump() for d in collateral_engine.promotional_debts.values()],
                    "liquidity_targets": [lt.model_dump() for lt in liquidity_targets],
                    "external_liquid_backstop": float(collateral_engine.external_liquid_backstop),
                })

            update_json(self.state_file, mutate, default={})
            logger.info(f"State atomically updated to {self.state_file}")

        await asyncio.to_thread(merge_and_persist)
        return snapshot, False

