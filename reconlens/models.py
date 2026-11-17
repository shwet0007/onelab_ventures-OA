"""Shared result structures and constants for ReconLens."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

FEE_RATE = Decimal("0.015")
MONEY_QUANTUM = Decimal("0.01")
VALID_TRANSACTION_TYPES = {"PAYMENT", "REFUND"}
VALID_STATUSES = {"SUCCESS"}


class ReconciliationValidationError(ValueError):
    """Raised when source data is unsafe to reconcile."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Validation failed:\n- " + "\n- ".join(errors))


@dataclass(frozen=True)
class FinancialBridge:
    platform_expected_net: Decimal
    raw_october_bank_net: Decimal
    raw_difference: Decimal
    cross_month_adjustment: Decimal
    rounding_adjustment: Decimal
    duplicate_adjustment: Decimal
    reconciled_expected_bank_net: Decimal
    unexplained_residual: Decimal


@dataclass
class ReconciliationResult:
    bridge: FinancialBridge
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    duplicate_rows: list[dict[str, Any]] = field(default_factory=list)
    matched_transaction_ids: list[str] = field(default_factory=list)
    rounding_affected_transactions: int = 0

    def exception_count(self, exception_type: str) -> int:
        return sum(e["exception_type"] == exception_type for e in self.exceptions)
