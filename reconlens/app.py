"""Streamlit dashboard for ReconLens."""

from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

from data_generator import generate_data
from models import ReconciliationValidationError
from reconciliation import load_csvs, reconcile

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def inr(value: Decimal) -> str:
    return f"₹{value:,.2f}"


def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8")


st.set_page_config(page_title="ReconLens", page_icon="🔎", layout="wide")
st.title("ReconLens")
st.subheader("Payment Reconciliation Dashboard")
st.caption("Reporting period: October 2026 · Currency: INR · Synthetic assessment data")

if not (DATA_DIR / "platform_transactions.csv").exists() or not (DATA_DIR / "bank_settlements.csv").exists():
    generate_data()

try:
    platform, bank = load_csvs(str(DATA_DIR / "platform_transactions.csv"), str(DATA_DIR / "bank_settlements.csv"))
    result = reconcile(platform, bank, "2026-10")
except (OSError, ReconciliationValidationError) as exc:
    st.error(f"Reconciliation could not run: {exc}")
    st.stop()

bridge = result.bridge
st.header("Summary")
columns = st.columns(4)
columns[0].metric("Platform Expected Net", inr(bridge.platform_expected_net))
columns[1].metric("Raw October Bank Net", inr(bridge.raw_october_bank_net))
columns[2].metric("Raw Difference", inr(bridge.raw_difference))
columns[3].metric("Unexplained Residual", inr(bridge.unexplained_residual))
if bridge.unexplained_residual == 0:
    st.success("The financial difference is fully explained. Operational exceptions still require review.")
else:
    st.warning("An unexplained financial residual remains and requires investigation.")

st.header("Financial Reconciliation Bridge")
st.write("This bridge explains the difference between expected platform funds and the bank's reported October settlements.")
bridge_rows = [
    ("Platform Expected Net", bridge.platform_expected_net),
    ("Cross-Month Adjustment", bridge.cross_month_adjustment),
    ("Rounding Adjustment", bridge.rounding_adjustment),
    ("Duplicate Adjustment", bridge.duplicate_adjustment),
    ("Adjusted Expected Bank Net", bridge.reconciled_expected_bank_net),
    ("Actual Bank Net", bridge.raw_october_bank_net),
    ("Unexplained Residual", bridge.unexplained_residual),
]
st.dataframe(pd.DataFrame([{"Bridge Item": label, "Amount (INR)": inr(value)} for label, value in bridge_rows]), use_container_width=True, hide_index=True)

st.header("Discrepancy Overview")
overview = st.columns(4)
overview[0].metric("Cross-Month Settlements", result.exception_count("CROSS_MONTH_SETTLEMENT"))
overview[1].metric("Rounding Variance Groups", result.exception_count("ROUNDING_VARIANCE"))
overview[2].metric("Duplicate Bank Entries", result.exception_count("DUPLICATE_BANK_ENTRY"))
overview[3].metric("Orphan Refunds", result.exception_count("ORPHAN_REFUND"))
st.caption(f"The rounding group contains {result.rounding_affected_transactions} affected transactions.")

st.header("Exception Investigation")
exceptions = pd.DataFrame(result.exceptions)
type_options = sorted(exceptions["exception_type"].unique()) if not exceptions.empty else []
status_options = sorted(exceptions["resolution_status"].unique()) if not exceptions.empty else []
filter_columns = st.columns(2)
selected_types = filter_columns[0].multiselect("Exception type", type_options, default=type_options)
selected_statuses = filter_columns[1].multiselect("Resolution status", status_options, default=status_options)
filtered = exceptions[exceptions["exception_type"].isin(selected_types) & exceptions["resolution_status"].isin(selected_statuses)].copy()
for name in ("platform_amount", "bank_amount", "financial_impact"):
    filtered[name] = filtered[name].map(lambda value: "" if value is None else inr(value))
display_columns = {
    "transaction_id": "Transaction ID", "exception_type": "Exception Type",
    "platform_amount": "Expected Amount", "bank_amount": "Actual Amount",
    "financial_impact": "Financial Impact", "explanation": "Explanation",
    "resolution_status": "Resolution Status", "recommended_action": "Recommended Action",
}
st.dataframe(filtered[list(display_columns)].rename(columns=display_columns), use_container_width=True, hide_index=True)

st.header("Synthetic Dataset Preview")
platform_tab, bank_tab = st.tabs([f"Platform transactions ({len(platform)})", f"Bank settlements ({len(bank)})"])
with platform_tab: st.dataframe(platform, use_container_width=True, hide_index=True)
with bank_tab: st.dataframe(bank, use_container_width=True, hide_index=True)

st.header("Downloads")
downloads = st.columns(3)
downloads[0].download_button("Platform CSV", csv_bytes(platform), "platform_transactions.csv", "text/csv")
downloads[1].download_button("Bank CSV", csv_bytes(bank), "bank_settlements.csv", "text/csv")
downloads[2].download_button("Exceptions CSV", csv_bytes(exceptions), "reconciliation_exceptions.csv", "text/csv")

with st.expander("Assumptions and limitations"):
    st.markdown("""
- All records use INR; October 2026 is the reporting period and November data is available for delayed-settlement lookup.
- Payments are positive, refunds are negative, payment fees are 1.5%, and refund fees are zero.
- Platform fees are aggregated at full precision and rounded once; bank fees are rounded per transaction using `ROUND_HALF_UP`.
- Settlement IDs identify logical bank records in this synthetic model; repeated IDs are retained and flagged.
- Matching assumes one transaction per settlement and uses transaction ID, type, currency, and merchant.
- Real banks may settle batches and apply variable fees, taxes, FX, corrections, reversals, or longer delays.
- Missing refund history can create a false orphan classification. A balanced bridge does not prove every transaction is legitimate.
- Production systems require idempotent ingestion, audit trails, controlled exception resolution, and monitoring.
""")
