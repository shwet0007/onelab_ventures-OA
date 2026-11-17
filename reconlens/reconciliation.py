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
        if pd.isna(row["currency"]) or not str(row["currency"]).strip(): errors.append(f"{prefix} currency is missing")
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


def _expected_amounts(platform_row: pd.Series) -> tuple[Decimal, Decimal, Decimal]:
    expected_fee_exact = platform_row["_amount"] * FEE_RATE if platform_row["transaction_type"] == "PAYMENT" else Decimal("0")
    expected_fee_bank = expected_fee_exact.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    expected_net_bank = platform_row["_amount"] - expected_fee_bank
    return expected_fee_exact, expected_fee_bank, expected_net_bank


def _amounts_match(platform_row: pd.Series, bank_row: pd.Series) -> bool:
    _, expected_fee, expected_net = _expected_amounts(platform_row)
    return (
        bank_row["_gross_amount"] == platform_row["_amount"]
        and bank_row["_fee_amount"] == expected_fee
        and bank_row["_net_amount"] == expected_net
    )


def _identity_matches(platform_row: pd.Series, bank_row: pd.Series) -> bool:
    return (
        bank_row["transaction_type"] == platform_row["transaction_type"]
        and bank_row["currency"] == platform_row["currency"]
        and bank_row["merchant_id"] == platform_row["merchant_id"]
    )


def reconcile(platform_df: pd.DataFrame, bank_df: pd.DataFrame, reporting_month: str = "2026-10") -> ReconciliationResult:
    p, b = _validate(platform_df, bank_df)
    year, month = map(int, reporting_month.split("-"))
    in_period = lambda d: d.year == year and d.month == month
    platform_period = p[p["_date"].map(in_period)].copy()
    bank_period = b[b["_date"].map(in_period)].copy()

    duplicate_mask = b["settlement_id"].astype(str).duplicated(keep="first")
    duplicate_rows_df = b[duplicate_mask].copy()
    canonical = b[~duplicate_mask].copy()
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
    consumed_bank_source_rows: set[int] = set()
    multiple_settlement_source_rows: set[int] = set()
    cross_adjustment = Decimal("0.00")
    rounding_pairs: list[tuple[Decimal, Decimal]] = []

    for transaction_id, prow in p_by_id.items():
        candidates = [r for r in bank_by_id.get(transaction_id, []) if _identity_matches(prow, r)]
        current = [r for r in candidates if in_period(r["_date"])]
        later = [r for r in candidates if r["_date"] > date(year, month, 1) and not in_period(r["_date"])]
        expected_fee_exact, _, expected_net_bank = _expected_amounts(prow)

        # Prefer a correctly valued row, then source order. Any other distinct
        # settlement ID remains unconsumed and is reported below.
        eligible = current or later
        selected = min(eligible, key=lambda r: (not _amounts_match(prow, r), int(r["source_row"]))) if eligible else None
        if selected is not None:
            consumed_bank_source_rows.add(int(selected["source_row"]))
            matched.append(transaction_id)
            amount_match = _amounts_match(prow, selected)
            if in_period(selected["_date"]):
                if prow["transaction_type"] == "PAYMENT" and amount_match:
                    rounding_pairs.append((expected_fee_exact, selected["_fee_amount"]))
                elif not amount_match:
                    impact = expected_net_bank - selected["_net_amount"]
                    exceptions.append(_exception("AMOUNT_OR_FEE_MISMATCH", transaction_id, str(selected["settlement_id"]), expected_net_bank, selected["_net_amount"], impact, "Bank gross, fee, or net does not follow the expected transaction-level rule.", amount_difference=impact))
            else:
                adjustment = -expected_net_bank
                cross_adjustment += adjustment
                if amount_match:
                    explanation = "Payment was processed in October and validly settled in a later month. This explains a timing difference."
                    status = "EXPLAINED"
                    action = "Monitor settlement timing; no amount correction is required."
                else:
                    explanation = "Payment was processed in October and appears in a later month, but the later settlement values do not reconcile. The timing adjustment remains valid; the settlement amount requires review."
                    status = "REQUIRES_REVIEW"
                    action = "Investigate and correct the later-period settlement values."
                exceptions.append(_exception("CROSS_MONTH_SETTLEMENT", transaction_id, str(selected["settlement_id"]), expected_net_bank, selected["_net_amount"], adjustment, explanation, status, action))
                if not amount_match:
                    amount_difference = expected_net_bank - selected["_net_amount"]
                    exceptions.append(_exception("AMOUNT_OR_FEE_MISMATCH", transaction_id, str(selected["settlement_id"]), expected_net_bank, selected["_net_amount"], Decimal("0.00"), "The later-period settlement gross, fee, or net does not follow the expected transaction-level rule. It does not change the October raw bank total.", amount_difference=amount_difference))
        else:
            exceptions.append(_exception("UNMATCHED_PLATFORM_TRANSACTION", transaction_id, platform_amount=expected_net_bank, financial_impact=expected_net_bank, explanation="No bank record with matching transaction type, currency, and merchant was found in October or the available later data."))

        if selected is not None:
            for extra in candidates:
                extra_source_row = int(extra["source_row"])
                if extra_source_row == int(selected["source_row"]):
                    continue
                multiple_settlement_source_rows.add(extra_source_row)
                impact = -extra["_net_amount"] if in_period(extra["_date"]) else Decimal("0.00")
                exceptions.append(_exception("MULTIPLE_BANK_SETTLEMENTS", transaction_id, str(extra["settlement_id"]), expected_net_bank, extra["_net_amount"], impact, "A distinct additional settlement ID references a platform transaction that already consumed another bank settlement. The additional row is preserved but not treated as a valid one-to-one match.", action="Determine whether the additional settlement is a correction, reversal, or duplicate transfer.", source_row=int(extra["source_row"])))

        if prow["transaction_type"] == "REFUND":
            original = str(prow["original_transaction_id"]).strip()
            original_exists = original in set(p["transaction_id"].astype(str)) and any((p["transaction_id"].astype(str) == original) & (p["transaction_type"] == "PAYMENT"))
            if not original_exists:
                exceptions.append(_exception("ORPHAN_REFUND", transaction_id, platform_amount=prow["_amount"], bank_amount=selected["_net_amount"] if selected is not None else None, financial_impact=Decimal("0.00"), explanation="The refund matches financially but references an original payment missing from the available platform records.", action="Verify the original payment in historical records before approving the refund."))

    for _, brow in canonical.iterrows():
        bank_source_row = int(brow["source_row"])
        if bank_source_row in consumed_bank_source_rows or bank_source_row in multiple_settlement_source_rows:
            continue
        in_reporting_period = in_period(brow["_date"])
        impact = -brow["_net_amount"] if in_reporting_period else Decimal("0.00")
        platform_row = p_by_id.get(str(brow["transaction_id"]))
        if platform_row is None:
            explanation = "Bank settlement has no matching platform transaction."
        else:
            mismatched_fields = [name for name in ("transaction_type", "currency", "merchant_id") if brow[name] != platform_row[name]]
            explanation = f"Bank settlement was not consumed because it differs from the platform record on: {', '.join(mismatched_fields)}." if mismatched_fields else "Bank settlement was not consumed by a valid one-to-one reconciliation match."
        exceptions.append(_exception("UNMATCHED_BANK_SETTLEMENT", str(brow["transaction_id"]), str(brow["settlement_id"]), bank_amount=brow["_net_amount"], financial_impact=impact, explanation=explanation, source_row=int(brow["source_row"])))

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
