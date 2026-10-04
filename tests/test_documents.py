from __future__ import annotations

import asyncio
import inspect
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.atomic_io import atomic_write_json, read_json
from core.collateral_engine import CollateralEngine
from core.liquidity_models import (
    CollateralInvariantState,
    DocumentClass,
    DocumentLineItem,
    PromotionalDebt,
    UnifiedDocumentSnapshot,
    extract_snapshot,
)
from core.paths import MACRO_STATE_FILE, PROCESSED_HASHES_FILE
from services.document_parser import DocumentExtractionError, DocumentParser

D = Decimal

SNAPSHOT_JSON = {
    "document_class": "CREDIT_CARD_STATEMENT",
    "institution_or_bureau": "CHASE",
    "report_date": "2026-10-01",
    "line_items": [
        {"account_name": "CHASE SLATE", "current_balance": 1800.0, "monthly_payment": 45.0, "is_promotional": False}
    ],
}


class FakeModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=json.dumps(reply))


class SchemaRejected(Exception):
    code = 400


def parser_with(replies, cash=(D("720"), D("240"))) -> tuple[DocumentParser, FakeModels]:
    parser = DocumentParser(cash_provider=lambda: cash)
    models = FakeModels(replies)
    parser.client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return parser, models


def doc(tmp_path: Path, name="stmt.pdf", content=b"%PDF-1.4 fake statement") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


def run(coro):
    return asyncio.run(coro)


def test_client_timeout_is_120_seconds_in_milliseconds():
    src = inspect.getsource(DocumentParser.__init__)
    assert '"timeout": 120_000' in src


def test_sha256_duplicate_is_detected_before_a_second_model_call(tmp_path):
    parser, models = parser_with([SNAPSHOT_JSON])
    f = doc(tmp_path)

    snap, dup = run(parser.extract_document(f))
    assert dup is False and snap.document_class == DocumentClass.CREDIT_CARD_STATEMENT
    assert len(models.calls) == 1
    assert len(read_json(PROCESSED_HASHES_FILE)) == 1

    again, dup2 = run(parser.extract_document(f))
    assert dup2 is True and again.institution_or_bureau == "CHASE"
    assert len(models.calls) == 1, "duplicate file must not trigger another (paid) model call"


def test_extraction_failure_raises_and_persists_nothing(tmp_path):
    parser, _ = parser_with([RuntimeError("deadline exceeded")])
    with pytest.raises(DocumentExtractionError, match="deadline exceeded"):
        run(parser.extract_document(doc(tmp_path)))
    assert not MACRO_STATE_FILE.exists()
    assert read_json(PROCESSED_HASHES_FILE, default={}) == {}


def test_missing_client_raises_instead_of_inventing_data(tmp_path):
    parser = DocumentParser()
    parser.client = None
    parser.allow_sample_fallback = False
    with pytest.raises(DocumentExtractionError, match="not configured"):
        run(parser.extract_document(doc(tmp_path)))
    assert not MACRO_STATE_FILE.exists()


def test_schema_rejection_retries_once_in_plain_json_mode(tmp_path):
    parser, models = parser_with([SchemaRejected("bad schema"), SNAPSHOT_JSON])
    snap, dup = run(parser.extract_document(doc(tmp_path)))
    assert dup is False and snap.institution_or_bureau == "CHASE"
    assert len(models.calls) == 2
    first, second = models.calls
    assert first["config"].response_schema is UnifiedDocumentSnapshot
    assert second["config"].response_schema is None
    assert "JSON Schema" in second["contents"][1]


def test_non_schema_errors_are_not_retried(tmp_path):
    parser, models = parser_with([TimeoutError("read timeout"), SNAPSHOT_JSON])
    with pytest.raises(DocumentExtractionError):
        run(parser.extract_document(doc(tmp_path)))
    assert len(models.calls) == 1


def test_manual_promo_debt_backstop_and_targets_survive_ingestion_and_merge(tmp_path):
    manual = PromotionalDebt(
        id="manual_chase", institution="Chase", total_balance=D("2500.00"),
        expiration_date=date.today() + timedelta(days=45), is_manual=True, notes="0% APR through Nov",
    )
    atomic_write_json(MACRO_STATE_FILE, {
        "promotional_debts": [manual.model_dump()],
        "external_liquid_backstop": 5000.0,
        "liquidity_targets": [{
            "target_id": "tax", "label": "Q4 tax", "target_amount": 1200.0,
            "target_date": "2026-12-15", "is_active": True, "created_at": "2026-10-01",
        }],
    })
    parser, _ = parser_with([SNAPSHOT_JSON], cash=(D("720.00"), D("240.00")))
    run(parser.extract_document(doc(tmp_path)))

    saved = read_json(MACRO_STATE_FILE)
    debt = saved["promotional_debts"][0]
    assert debt["id"] == "manual_chase" and debt["is_manual"] is True
    assert debt["total_balance"] == 1800.0, "bureau/statement balance merges into the manual record"
    assert debt["expiration_date"] == manual.expiration_date.isoformat(), "bureau reports carry no promo expiry"
    assert saved["external_liquid_backstop"] == 5000.0
    assert saved["liquidity_targets"][0]["target_id"] == "tax"

    state = saved["collateral_state"]
    assert state["total_liquid_backstop"] == 720 + 240 + 5000
    assert state["net_collateral_buffer"] == (720 + 240 + 5000) - 1800
    assert state["is_solvent"] is True


def test_net_collateral_buffer_formula_includes_external_backstop():
    s = CollateralInvariantState.calculate(
        settled_cash=D("100"), unsettled_cash=D("50"), active_promotional_debt=D("400"),
        external_liquid_backstop=D("200"),
    )
    assert s.total_liquid_backstop == D("350")
    assert s.net_collateral_buffer == D("-50")
    assert s.is_solvent is False
    default_backstop = CollateralInvariantState.calculate(D("10"), D("0"), D("5"))
    assert default_backstop.external_liquid_backstop == D("0.00") and default_backstop.is_solvent is True


def test_generalised_snapshot_accepts_paystub_and_student_loan_without_credit_fields():
    paystub = UnifiedDocumentSnapshot(
        document_class="PAYSTUB", institution_or_bureau="ACME PAYROLL", report_date="2026-10-01",
        line_items=[DocumentLineItem(account_name="Net pay", current_balance=D("2150.42"), notes="biweekly")],
    )
    loan = UnifiedDocumentSnapshot(
        document_class="STUDENT_LOAN_STATEMENT", institution_or_bureau="NELNET", report_date="2026-09-28",
        line_items=[DocumentLineItem(account_name="Loan 1", current_balance=D("18000"), monthly_payment=D("210"))],
    )
    assert paystub.total_revolving_limit == D("0.00") and loan.line_items[0].monthly_payment == D("210")
    assert extract_snapshot({"snapshot": json.loads(paystub.model_dump_json())}).document_class == DocumentClass.PAYSTUB


def test_legacy_credit_report_shape_is_still_readable():
    legacy = {"credit_report": {
        "bureau": "EXPERIAN", "report_date": "2026-09-01",
        "tradelines": [{"account_name": "CITI", "current_balance": 100, "is_promotional": False}],
    }}
    snap = extract_snapshot(legacy)
    assert snap is not None and snap.document_class == DocumentClass.CREDIT_REPORT
    assert extract_snapshot({"snapshot": {"broken": True}}) is None


def test_collateral_engine_promotional_debt_manual_entry_and_merge():
    eng = CollateralEngine(external_liquid_backstop=D("100"))
    eng.add_or_update_promotional_debt(PromotionalDebt(
        id="m1", institution="Discover", total_balance=D("900"), expiration_date=date.today() + timedelta(days=10),
        is_manual=True))
    eng.merge_document_snapshot(UnifiedDocumentSnapshot(
        document_class="CREDIT_REPORT", institution_or_bureau="EXPERIAN", report_date="2026-10-01",
        line_items=[DocumentLineItem(account_name="DISCOVER IT", current_balance=D("700"), monthly_payment=D("25"))]))
    debt = eng.promotional_debts["m1"]
    assert debt.total_balance == D("700") and debt.minimum_monthly_payment == D("25") and debt.is_manual
    assert eng.get_total_active_promotional_debt() == D("700")
    assert eng.evaluate_invariant(D("0"), D("0")).net_collateral_buffer == D("-600")
