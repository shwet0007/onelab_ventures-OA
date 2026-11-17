from decimal import Decimal

import pandas as pd

from data_generator import generate_data


def test_generator_counts_and_dates(tmp_path):
    platform_path, bank_path = generate_data(tmp_path)
    platform = pd.read_csv(platform_path, dtype=str, keep_default_na=False)
    bank = pd.read_csv(bank_path, dtype=str, keep_default_na=False)
    assert len(platform) == 204
    assert len(bank) == 205
    assert bank["settlement_date"].str.startswith("2026-10").sum() == 204
    assert bank["settlement_date"].str.startswith("2026-11").sum() == 1
    assert platform["transaction_id"].is_unique


def test_generator_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    paths_a = generate_data(first)
    paths_b = generate_data(second)
    assert paths_a[0].read_bytes() == paths_b[0].read_bytes()
    assert paths_a[1].read_bytes() == paths_b[1].read_bytes()


def test_ordinary_amount_distribution_and_bank_arithmetic(tmp_path):
    platform_path, bank_path = generate_data(tmp_path)
    platform = pd.read_csv(platform_path, dtype=str, keep_default_na=False)
    bank = pd.read_csv(bank_path, dtype=str, keep_default_na=False)
    ordinary = platform[platform["transaction_id"].str.startswith("ORD")]
    assert ordinary["amount"].value_counts().to_dict() == {"101.00": 50, "103.00": 50, "105.00": 50, "107.00": 50}
    for row in bank.to_dict("records"):
        assert Decimal(row["gross_amount"]) - Decimal(row["fee_amount"]) == Decimal(row["net_amount"])
