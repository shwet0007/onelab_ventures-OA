"""Decimal-safe, explainable payment reconciliation engine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import pandas as pd

from models import (
    FEE_RATE, MONEY_QUANTUM, VALID_STATUSES, VALID_TRANSACTION_TYPES,
    FinancialBridge, ReconciliationResult, ReconciliationValidationError,
)

PLATFORM_COLUMNS = {"transaction_id", "transaction_date", "transaction_type", "amount", "currency", "status", "original_transaction_id", "merchant_id"}
BANK_COLUMNS = {"settlement_id", "transaction_id", "settlement_date", "transaction_type", "gross_amount", "fee_amount", "net_amount", "currency", "merchant_id"}


def _decimal(value: Any, label: str, errors: list[str]) -> Decimal | None:
    if value is None or pd.isna(value) or str(value).strip() == "":
        errors.append(f"{label} is missing")
        return None
    try:
        result = Decimal(str(value).strip())
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        errors.append(f"{label} is not a valid monetary value: {value!r}")
        return None


def _date(value: Any, label: str, errors: list[str]) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        errors.append(f"{label} is not a valid ISO date: {value!r}")
        return None


def _validate(platform: pd.DataFrame, bank: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    errors: list[str] = []
    missing_platform = sorted(PLATFORM_COLUMNS - set(platform.columns))
    missing_bank = sorted(BANK_COLUMNS - set(bank.columns))
    if missing_platform:
        errors.append(f"Platform is missing required columns: {', '.join(missing_platform)}")
    if missing_bank:
        errors.append(f"Bank is missing required columns: {', '.join(missing_bank)}")
    if errors:
        raise ReconciliationValidationError(errors)

    p, b = platform.copy(deep=True), bank.copy(deep=True)
    p["source_row"] = range(2, len(p) + 2)
    b["source_row"] = range(2, len(b) + 2)
    for idx, row in p.iterrows():
        prefix = f"Platform source row {row['source_row']}"
        if pd.isna(row["transaction_id"]) or not str(row["transaction_id"]).strip(): errors.append(f"{prefix} transaction_id is missing")
        p.at[idx, "_date"] = _date(row["transaction_date"], f"{prefix} transaction_date", errors)
        p.at[idx, "_amount"] = _decimal(row["amount"], f"{prefix} amount", errors)
        if row["transaction_type"] not in VALID_TRANSACTION_TYPES: errors.append(f"{prefix} has invalid transaction_type {row['transaction_type']!r}")
        if row["status"] not in VALID_STATUSES: errors.append(f"{prefix} has invalid status {row['status']!r}")
        if row["currency"] != "INR": errors.append(f"{prefix} must use INR")
        amount = p.at[idx, "_amount"]
        if isinstance(amount, Decimal):
            if row["transaction_type"] == "PAYMENT" and amount <= 0: errors.append(f"{prefix} PAYMENT amount must be positive")
            if row["transaction_type"] == "REFUND" and amount >= 0: errors.append(f"{prefix} REFUND amount must be negative")
    duplicates = p[p["transaction_id"].astype(str).duplicated(keep=False)]["transaction_id"].astype(str).unique()
    if len(duplicates): errors.append(f"Duplicate platform transaction_id values: {', '.join(duplicates)}")

    for idx, row in b.iterrows():
        prefix = f"Bank source row {row['source_row']}"
        for name in ("settlement_id", "transaction_id"):
            if pd.isna(row[name]) or not str(row[name]).strip(): errors.append(f"{prefix} {name} is missing")
        b.at[idx, "_date"] = _date(row["settlement_date"], f"{prefix} settlement_date", errors)
        for name in ("gross_amount", "fee_amount", "net_amount"):
            b.at[idx, f"_{name}"] = _decimal(row[name], f"{prefix} {name}", errors)
        if row["transaction_type"] not in VALID_TRANSACTION_TYPES: errors.append(f"{prefix} has invalid transaction_type {row['transaction_type']!r}")
        if row["currency"] != "INR": errors.append(f"{prefix} must use INR")
        gross, fee, net = (b.at[idx, f"_{x}"] for x in ("gross_amount", "fee_amount", "net_amount"))
        if all(isinstance(x, Decimal) for x in (gross, fee, net)):
            if fee < 0: errors.append(f"{prefix} fee_amount must be nonnegative")
            if gross - fee != net: errors.append(f"{prefix} violates net_amount = gross_amount - fee_amount")
            if row["transaction_type"] == "PAYMENT" and gross <= 0: errors.append(f"{prefix} PAYMENT gross must be positive")
            if row["transaction_type"] == "REFUND" and (gross >= 0 or fee != 0): errors.append(f"{prefix} REFUND must have negative gross and zero fee")
    if errors:
        raise ReconciliationValidationError(errors)
    return p, b


def _exception(kind: str, transaction_id: str = "", settlement_id: str = "", platform_amount: Decimal | None = None,
               bank_amount: Decimal | None = None, financial_impact: Decimal = Decimal("0.00"), explanation: str = "",
               status: str = "REQUIRES_REVIEW", action: str = "Investigate and document resolution.", **extra: Any) -> dict[str, Any]:
    result = {
        "exception_id": "", "exception_type": kind, "transaction_id": transaction_id,
        "settlement_id": settlement_id, "platform_amount": platform_amount, "bank_amount": bank_amount,
        "financial_impact": financial_impact, "explanation": explanation,
        "resolution_status": status, "recommended_action": action,
    }
    result.update(extra)
    return result


def reconcile(platform_df: pd.DataFrame, bank_df: pd.DataFrame, reporting_month: str = "2026-10") -> ReconciliationResult:
    p, b = _validate(platform_df, bank_df)
    year, month = map(int, reporting_month.split("-"))
    in_period = lambda d: d.year == year and d.month == month
    platform_period = p[p["_date"].map(in_period)].copy()
    bank_period = b[b["_date"].map(in_period)].copy()

    duplicate_mask = b["settlement_id"].astype(str).duplicated(keep="first")
    duplicate_rows_df = b[duplicate_mask].copy()
    canonical = b[~duplicate_mask].copy()
    canonical_period = canonical[canonical["_date"].map(in_period)].copy()
    exceptions: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    for _, row in duplicate_rows_df.iterrows():
        item = {"settlement_id": row["settlement_id"], "transaction_id": row["transaction_id"], "source_row": int(row["source_row"]), "duplicate_net_amount": row["_net_amount"], "explanation": "Repeated settlement ID; source row retained but excluded from canonical matching."}
        duplicate_rows.append(item)
        exceptions.append(_exception("DUPLICATE_BANK_ENTRY", str(row["transaction_id"]), str(row["settlement_id"]), bank_amount=row["_net_amount"], financial_impact=row["_net_amount"], explanation="The same settlement reference appears more than once. The extra source row inflates the raw bank total.", action="Confirm whether this is a duplicate extract row, correction, or real transfer.", source_row=int(row["source_row"])))

    p_by_id = {str(row["transaction_id"]): row for _, row in platform_period.iterrows()}
    bank_by_id: dict[str, list[pd.Series]] = {}
    for _, row in canonical.iterrows(): bank_by_id.setdefault(str(row["transaction_id"]), []).append(row)
    matched: list[str] = []
    cross_adjustment = Decimal("0.00")
    rounding_pairs: list[tuple[Decimal, Decimal]] = []

    for transaction_id, prow in p_by_id.items():
        candidates = [r for r in bank_by_id.get(transaction_id, []) if r["transaction_type"] == prow["transaction_type"] and r["currency"] == prow["currency"] and r["merchant_id"] == prow["merchant_id"]]
        current = [r for r in candidates if in_period(r["_date"])]
        later = [r for r in candidates if r["_date"] > date(year, month, 1) and not in_period(r["_date"])]
        expected_fee = prow["_amount"] * FEE_RATE if prow["transaction_type"] == "PAYMENT" else Decimal("0")
        expected_net_exact = prow["_amount"] - expected_fee
        if current:
            brow = current[0]
            matched.append(transaction_id)
            valid_bank_fee = expected_fee.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            gross_match = brow["_gross_amount"] == prow["_amount"]
            fee_match = brow["_fee_amount"] == valid_bank_fee
            net_match = brow["_net_amount"] == prow["_amount"] - valid_bank_fee
            if prow["transaction_type"] == "PAYMENT" and gross_match and fee_match and net_match:
                rounding_pairs.append((expected_fee, brow["_fee_amount"]))
            elif not (gross_match and fee_match and net_match):
                impact = expected_net_exact.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP) - brow["_net_amount"]
                exceptions.append(_exception("AMOUNT_OR_FEE_MISMATCH", transaction_id, str(brow["settlement_id"]), expected_net_exact.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), brow["_net_amount"], impact, "Bank gross, fee, or net does not follow the expected transaction-level rule."))
        elif later:
            brow = sorted(later, key=lambda r: r["_date"])[0]
            adjustment = -expected_net_exact.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            cross_adjustment += adjustment
            exceptions.append(_exception("CROSS_MONTH_SETTLEMENT", transaction_id, str(brow["settlement_id"]), -adjustment, brow["_net_amount"], adjustment, "Payment was processed in October but settled in a later month. This explains a timing difference.", "EXPLAINED", "Monitor settlement timing; no amount correction is required."))
        else:
            exceptions.append(_exception("UNMATCHED_PLATFORM_TRANSACTION", transaction_id, platform_amount=expected_net_exact.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), financial_impact=expected_net_exact.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), explanation="No matching bank settlement was found in October or the available later data."))

        if prow["transaction_type"] == "REFUND":
            original = str(prow["original_transaction_id"]).strip()
            original_exists = original in set(p["transaction_id"].astype(str)) and any((p["transaction_id"].astype(str) == original) & (p["transaction_type"] == "PAYMENT"))
            if not original_exists:
                exceptions.append(_exception("ORPHAN_REFUND", transaction_id, platform_amount=prow["_amount"], bank_amount=current[0]["_net_amount"] if current else None, financial_impact=Decimal("0.00"), explanation="The refund matches financially but references an original payment missing from the available platform records.", action="Verify the original payment in historical records before approving the refund."))

    for _, brow in canonical_period.iterrows():
        if str(brow["transaction_id"]) not in p_by_id:
            exceptions.append(_exception("UNMATCHED_BANK_SETTLEMENT", str(brow["transaction_id"]), str(brow["settlement_id"]), bank_amount=brow["_net_amount"], financial_impact=-brow["_net_amount"], explanation="October bank settlement has no matching platform transaction."))

    exact_fee_total = sum((fee for fee, _ in rounding_pairs), Decimal("0")).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    bank_fee_total = sum((fee for _, fee in rounding_pairs), Decimal("0.00"))
    rounding_variance = bank_fee_total - exact_fee_total
    rounding_adjustment = -rounding_variance
    affected = sum(1 for exact, rounded in rounding_pairs if exact != rounded)
    if rounding_variance:
        exceptions.append(_exception("ROUNDING_VARIANCE", financial_impact=rounding_adjustment, explanation="Bank fees were rounded per transaction, while the platform rounded fees after aggregation.", status="EXPLAINED", action="Retain as a documented methodology variance.", affected_transactions=affected))

    payment_gross = sum((r["_amount"] for _, r in platform_period.iterrows() if r["transaction_type"] == "PAYMENT"), Decimal("0"))
    refunds = sum((r["_amount"] for _, r in platform_period.iterrows() if r["transaction_type"] == "REFUND"), Decimal("0"))
    aggregate_fee = (payment_gross * FEE_RATE).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    platform_expected = (payment_gross - aggregate_fee + refunds).quantize(MONEY_QUANTUM)
    raw_bank = sum((r["_net_amount"] for _, r in bank_period.iterrows()), Decimal("0.00")).quantize(MONEY_QUANTUM)
    duplicate_adjustment = sum((r["_net_amount"] for _, r in duplicate_rows_df.iterrows() if in_period(r["_date"])), Decimal("0.00"))
    adjusted = platform_expected + cross_adjustment + rounding_adjustment + duplicate_adjustment
    bridge = FinancialBridge(platform_expected, raw_bank, platform_expected - raw_bank, cross_adjustment, rounding_adjustment, duplicate_adjustment, adjusted, adjusted - raw_bank)
    for number, item in enumerate(exceptions, 1): item["exception_id"] = f"EXC-{number:04d}"
    return ReconciliationResult(bridge, exceptions, duplicate_rows, sorted(matched), affected)


def load_csvs(platform_path: str, bank_path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load source values as strings so decimal representations are preserved."""
    return pd.read_csv(platform_path, dtype=str, keep_default_na=False), pd.read_csv(bank_path, dtype=str, keep_default_na=False)
