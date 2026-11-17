# ReconLens

ReconLens is a local payment reconciliation dashboard for comparing platform transactions with bank settlements. It detects timing, rounding, duplicate-source, data-integrity, and unmatched-record exceptions while producing an auditable financial bridge.

## Business problem

Month-end platform expectations and bank-reported settlements can differ for legitimate or problematic reasons. A useful reconciliation must quantify the gap, explain it without discarding raw rows, and keep operational exceptions visible even when the financial residual is zero.

## Solution approach

The project generates a deterministic October 2026 fixture, validates both inputs, separates repeated settlement IDs from canonical matching records, matches transactions, searches November for delayed settlements, analyzes fee rounding, validates refund references, and builds a signed financial bridge. All money uses `Decimal`; CSV monetary fields are initially loaded as strings.

## Technology stack

- Python 3.11+
- pandas
- Streamlit
- pytest
- `decimal.Decimal` with `ROUND_HALF_UP`

No database, external API, authentication, or container runtime is required.

## Architecture

```text
reconlens/
├── app.py                     Streamlit presentation only
├── data_generator.py          Deterministic source fixtures
├── reconciliation.py          Validation and reconciliation rules
├── models.py                  Constants and result structures
├── data/
│   ├── platform_transactions.csv
│   └── bank_settlements.csv
└── tests/                     Engine and generator tests
```

The reconciliation engine is independent of Streamlit and does not mutate its input DataFrames.

## Dataset schemas

Platform fields: `transaction_id`, `transaction_date`, `transaction_type`, `amount`, `currency`, `status`, `original_transaction_id`, `merchant_id`.

Bank fields: `settlement_id`, `transaction_id`, `settlement_date`, `transaction_type`, `gross_amount`, `fee_amount`, `net_amount`, `currency`, `merchant_id`. Source row numbers are added internally for traceability.

## Synthetic discrepancy scenarios

- 200 ordinary payments expose a ₹1.00 aggregate fee-rounding methodology variance.
- `TXN002` is processed in October and confirmed as settled in November.
- A second source row for one settlement ID is retained and flagged as a duplicate bank entry.
- A financially matched refund references an original payment absent from available history.

## Assumptions

All records use INR. The reporting interval is October 1–31, 2026, with November available for delayed-settlement lookup. Each logical settlement references one platform transaction; platform IDs are unique. Payments are positive and refunds negative. Payment fees are 1.5%; refund fees are zero. Platform fees are summed at full precision then rounded once, while bank fees are rounded per transaction using `ROUND_HALF_UP`. Bank rows represent settled records. Duplicate settlement IDs represent repeated source records in this fixture. Available history is considered sufficient for the intentional orphan check. No prior-month settlements, FX, chargebacks, tax, or variable fees are modeled.

## Reconciliation methodology

1. Validate schemas, identifiers, dates, types, statuses, currencies, monetary values, signs, uniqueness, and bank arithmetic.
2. Preserve every bank row and identify additional occurrences of a settlement ID.
3. Match canonical records by transaction ID, type, currency, and merchant, recording the exact bank source row consumed by each one-to-one match. Incompatible or unconsumed bank rows remain visible as exceptions.
4. Search later available bank data before classifying an October platform record as unmatched, and validate the later settlement's gross, fee, net, type, currency, and merchant before describing it as fully explained.
5. Verify each bank fee against the valid transaction-level rounding rule, then isolate the legitimate aggregate methodology difference.
6. Validate refund references independently from financial matching.
7. Report unmatched rows, amount/fee mismatches, and additional distinct settlement IDs for an already matched platform transaction.

Malformed records raise clear validation errors and are never silently discarded.

## Financial reconciliation bridge

Signed adjustments transform platform expected net into expected raw October bank net:

```text
Platform expected net                 23881.75
Cross-month adjustment               -1970.00
Rounding adjustment                     -1.00
Duplicate-source adjustment            738.75
Adjusted expected / actual bank net   22649.50
Unexplained residual                      0.00
```

These values are calculated from the CSVs, not hardcoded in the engine.

## Installation and setup

From this directory, preferably inside a virtual environment:

```bash
python -m pip install -r requirements.txt
```

## Running the application

```bash
python data_generator.py
streamlit run app.py
```

The included data files allow the dashboard to work immediately. The app also regenerates them if they are absent.

## Running tests

```bash
python -m pytest -q
```

## Expected results

The fixture contains 204 platform rows and 205 bank rows: 204 October settlements and one November settlement. Expected detections are one cross-month settlement, one aggregate rounding group affecting 200 transactions, one additional duplicate row, and one orphan refund. The raw gap is ₹1,232.25 and the unexplained residual is ₹0.00. Duplicate and orphan issues remain review items.

## Production limitations

Real bank records may aggregate batches rather than map one-to-one. Refunds can legitimately reference older history. Repeated settlement IDs may be corrections or reversals. Processors can apply variable fees, taxes, exchange rates, and other adjustments, while settlement delays can exceed one month. Financial balance does not establish transaction legitimacy. A production system also needs idempotent ingestion, persistent audit trails, controlled exception workflows, access controls, and operational monitoring.

## Potential future improvements

Add configurable fee contracts, batch matching, multi-currency support, persistent reviewer workflows, ingestion controls, and longer settlement windows after validating those rules with stakeholders.

## AI-assisted development

This assessment used AI-assisted coding while keeping problem analysis, design decisions, implementation, and executed verification as distinct activities.
