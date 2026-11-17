from decimal import Decimal

import pandas as pd
import pytest

from data_generator import generate_data
from models import ReconciliationValidationError
from reconciliation import load_csvs, reconcile


@pytest.fixture()
def source_data(tmp_path):
    paths = generate_data(tmp_path)
    return load_csvs(str(paths[0]), str(paths[1]))


@pytest.fixture()
def result(source_data):
    return reconcile(*source_data, "2026-10")


def test_expected_bridge(result):
    bridge = result.bridge
    assert bridge.platform_expected_net == Decimal("23881.75")
    assert bridge.raw_october_bank_net == Decimal("22649.50")
    assert bridge.raw_difference == Decimal("1232.25")
    assert bridge.cross_month_adjustment == Decimal("-1970.00")
    assert bridge.rounding_adjustment == Decimal("-1.00")
    assert bridge.duplicate_adjustment == Decimal("738.75")
    assert bridge.reconciled_expected_bank_net == Decimal("22649.50")
    assert bridge.unexplained_residual == Decimal("0.00")


def test_expected_exception_counts(result):
    assert result.exception_count("CROSS_MONTH_SETTLEMENT") == 1
    assert result.exception_count("ROUNDING_VARIANCE") == 1
    assert result.exception_count("DUPLICATE_BANK_ENTRY") == 1
    assert result.exception_count("ORPHAN_REFUND") == 1
    assert result.rounding_affected_transactions == 200
    assert len(result.duplicate_rows) == 1
    assert "TXN001" in result.matched_transaction_ids
    assert not any(e["transaction_id"] == "TXN001" for e in result.exceptions)


def test_orphan_and_duplicate_remain_review_items_with_zero_residual(result):
    assert result.bridge.unexplained_residual == Decimal("0.00")
    review_types = {e["exception_type"] for e in result.exceptions if e["resolution_status"] == "REQUIRES_REVIEW"}
    assert {"DUPLICATE_BANK_ENTRY", "ORPHAN_REFUND"} <= review_types


def test_repeated_reconciliation_is_deterministic(source_data):
    first = reconcile(*source_data)
    second = reconcile(*source_data)
    assert first.bridge == second.bridge
    assert first.exceptions == second.exceptions


def test_additional_bank_only_record_is_detected(source_data):
    platform, bank = source_data
    extra = bank.iloc[0].copy()
    extra.update({"settlement_id": "SET-BANKONLY", "transaction_id": "BANKONLY", "gross_amount": "50.00", "fee_amount": "0.75", "net_amount": "49.25"})
    changed = pd.concat([bank, extra.to_frame().T], ignore_index=True)
    result = reconcile(platform, changed)
    assert result.exception_count("UNMATCHED_BANK_SETTLEMENT") == 1
    assert result.bridge.unexplained_residual == Decimal("-49.25")


def test_additional_platform_only_record_is_detected(source_data):
    platform, bank = source_data
    extra = platform.iloc[0].copy()
    extra.update({"transaction_id": "PLATFORMONLY", "amount": "100.00"})
    changed = pd.concat([platform, extra.to_frame().T], ignore_index=True)
    result = reconcile(changed, bank)
    assert result.exception_count("UNMATCHED_PLATFORM_TRANSACTION") == 1
    assert result.bridge.unexplained_residual == Decimal("98.50")


@pytest.mark.parametrize("value", ["", "invalid"])
def test_bad_money_is_validation_error(source_data, value):
    platform, bank = source_data
    changed = platform.copy()
    changed.loc[0, "amount"] = value
    with pytest.raises(ReconciliationValidationError, match="amount"):
        reconcile(changed, bank)


def test_duplicate_platform_id_is_validation_error(source_data):
    platform, bank = source_data
    changed = platform.copy()
    changed.loc[1, "transaction_id"] = changed.loc[0, "transaction_id"]
    with pytest.raises(ReconciliationValidationError, match="Duplicate platform"):
        reconcile(changed, bank)


def test_changed_valid_bank_amount_is_not_silently_accepted(source_data):
    platform, bank = source_data
    changed = bank.copy()
    row = changed.index[changed["transaction_id"] == "TXN001"][0]
    changed.loc[row, "gross_amount"] = "1001.00"
    changed.loc[row, "net_amount"] = "986.00"
    result = reconcile(platform, changed)
    assert result.exception_count("AMOUNT_OR_FEE_MISMATCH") == 1
    assert result.bridge.unexplained_residual != Decimal("0.00")


def test_existing_original_payment_means_refund_is_not_orphan(source_data):
    platform, bank = source_data
    changed = platform.copy()
    changed.loc[changed["transaction_id"] == "REF001", "original_transaction_id"] = "TXN001"
    result = reconcile(changed, bank)
    assert result.exception_count("ORPHAN_REFUND") == 0


def test_malformed_bank_arithmetic_is_rejected(source_data):
    platform, bank = source_data
    changed = bank.copy()
    changed.loc[0, "net_amount"] = "999.99"
    with pytest.raises(ReconciliationValidationError, match="net_amount = gross_amount - fee_amount"):
        reconcile(platform, changed)


def test_cross_month_settlement_amounts_are_validated_without_changing_october_bridge(source_data):
    platform, bank = source_data
    changed = bank.copy()
    row = changed.index[changed["transaction_id"] == "TXN002"][0]
    changed.loc[row, ["gross_amount", "fee_amount", "net_amount"]] = ["2100.00", "31.50", "2068.50"]

    result = reconcile(platform, changed)
    cross_month = next(e for e in result.exceptions if e["exception_type"] == "CROSS_MONTH_SETTLEMENT")
    mismatch = next(e for e in result.exceptions if e["exception_type"] == "AMOUNT_OR_FEE_MISMATCH" and e["transaction_id"] == "TXN002")

    assert cross_month["resolution_status"] == "REQUIRES_REVIEW"
    assert mismatch["amount_difference"] == Decimal("-98.50")
    assert mismatch["financial_impact"] == Decimal("0.00")
    assert result.bridge.platform_expected_net == Decimal("23881.75")
    assert result.bridge.raw_october_bank_net == Decimal("22649.50")
    assert result.bridge.cross_month_adjustment == Decimal("-1970.00")
    assert result.bridge.unexplained_residual == Decimal("0.00")


@pytest.mark.parametrize(
    ("field", "value", "amounts"),
    [
        ("transaction_type", "REFUND", ("-1000.00", "0.00", "-1000.00")),
        ("currency", "USD", None),
        ("merchant_id", "MERCHANT_OTHER", None),
    ],
)
def test_bank_record_with_incompatible_match_fields_remains_unmatched(source_data, field, value, amounts):
    platform, bank = source_data
    changed = bank.copy()
    row = changed.index[changed["transaction_id"] == "TXN001"][0]
    changed.loc[row, field] = value
    if amounts:
        changed.loc[row, ["gross_amount", "fee_amount", "net_amount"]] = amounts

    result = reconcile(platform, changed)
    unmatched_bank = [e for e in result.exceptions if e["exception_type"] == "UNMATCHED_BANK_SETTLEMENT" and e["settlement_id"] == "SET-TXN001"]

    assert len(unmatched_bank) == 1
    assert field in unmatched_bank[0]["explanation"]
    assert result.exception_count("UNMATCHED_PLATFORM_TRANSACTION") == 1
    assert "TXN001" not in result.matched_transaction_ids


def test_additional_distinct_settlement_for_same_transaction_is_reported(source_data):
    platform, bank = source_data
    extra = bank.loc[bank["transaction_id"] == "TXN001"].iloc[0].copy()
    extra["settlement_id"] = "SET-TXN001-SECOND"
    changed = pd.concat([bank, extra.to_frame().T], ignore_index=True)

    result = reconcile(platform, changed)
    multiple = [e for e in result.exceptions if e["exception_type"] == "MULTIPLE_BANK_SETTLEMENTS"]

    assert len(multiple) == 1
    assert multiple[0]["settlement_id"] == "SET-TXN001-SECOND"
    assert multiple[0]["financial_impact"] == Decimal("-985.00")
    assert result.exception_count("UNMATCHED_BANK_SETTLEMENT") == 0
    assert result.bridge.raw_october_bank_net == Decimal("23634.50")
    assert result.bridge.unexplained_residual == Decimal("-985.00")
