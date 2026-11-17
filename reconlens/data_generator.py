"""Create the deterministic ReconLens assessment fixture."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from models import FEE_RATE, MONEY_QUANTUM

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PLATFORM_FIELDS = [
    "transaction_id", "transaction_date", "transaction_type", "amount",
    "currency", "status", "original_transaction_id", "merchant_id",
]
BANK_FIELDS = [
    "settlement_id", "transaction_id", "settlement_date", "transaction_type",
    "gross_amount", "fee_amount", "net_amount", "currency", "merchant_id",
]


def money(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def build_datasets() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    platform: list[dict[str, str]] = []
    bank: list[dict[str, str]] = []
    amounts = [Decimal("101.00"), Decimal("103.00"), Decimal("105.00"), Decimal("107.00")]

    for index in range(1, 201):
        transaction_id = f"ORD{index:04d}"
        gross = amounts[(index - 1) % len(amounts)]
        transaction_date = date(2026, 10, 1) + timedelta(days=(index - 1) % 30)
        fee = (gross * FEE_RATE).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        platform.append({
            "transaction_id": transaction_id, "transaction_date": str(transaction_date),
            "transaction_type": "PAYMENT", "amount": money(gross), "currency": "INR",
            "status": "SUCCESS", "original_transaction_id": "", "merchant_id": "MERCHANT_001",
        })
        bank.append({
            "settlement_id": f"SET-{transaction_id}", "transaction_id": transaction_id,
            "settlement_date": str(transaction_date + timedelta(days=1)), "transaction_type": "PAYMENT",
            "gross_amount": money(gross), "fee_amount": money(fee), "net_amount": money(gross - fee),
            "currency": "INR", "merchant_id": "MERCHANT_001",
        })

    scenarios = [
        ("TXN001", "2026-10-15", "1000.00", "2026-10-16", "15.00"),
        ("TXN002", "2026-10-31", "2000.00", "2026-11-02", "30.00"),
        ("TXN003", "2026-10-20", "750.00", "2026-10-21", "11.25"),
    ]
    for transaction_id, txn_date, gross_text, settlement_date, fee_text in scenarios:
        gross, fee = Decimal(gross_text), Decimal(fee_text)
        platform.append({
            "transaction_id": transaction_id, "transaction_date": txn_date, "transaction_type": "PAYMENT",
            "amount": money(gross), "currency": "INR", "status": "SUCCESS",
            "original_transaction_id": "", "merchant_id": "MERCHANT_001",
        })
        settlement = {
            "settlement_id": f"SET-{transaction_id}", "transaction_id": transaction_id,
            "settlement_date": settlement_date, "transaction_type": "PAYMENT",
            "gross_amount": money(gross), "fee_amount": money(fee), "net_amount": money(gross - fee),
            "currency": "INR", "merchant_id": "MERCHANT_001",
        }
        bank.append(settlement)
        if transaction_id == "TXN003":
            bank.append(settlement.copy())

    platform.append({
        "transaction_id": "REF001", "transaction_date": "2026-10-22", "transaction_type": "REFUND",
        "amount": "-300.00", "currency": "INR", "status": "SUCCESS",
        "original_transaction_id": "TXN999", "merchant_id": "MERCHANT_001",
    })
    bank.append({
        "settlement_id": "SET-REF001", "transaction_id": "REF001", "settlement_date": "2026-10-23",
        "transaction_type": "REFUND", "gross_amount": "-300.00", "fee_amount": "0.00",
        "net_amount": "-300.00", "currency": "INR", "merchant_id": "MERCHANT_001",
    })
    return platform, bank


def generate_data(output_dir: Path | None = None) -> tuple[Path, Path]:
    output_dir = output_dir or DATA_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    platform, bank = build_datasets()
    platform_path, bank_path = output_dir / "platform_transactions.csv", output_dir / "bank_settlements.csv"
    for path, fields, rows in ((platform_path, PLATFORM_FIELDS, platform), (bank_path, BANK_FIELDS, bank)):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return platform_path, bank_path


if __name__ == "__main__":
    first, second = generate_data()
    print(f"Generated {first} and {second}")
