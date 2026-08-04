# Costing Tool

- secure sign-in using local hashed passwords or Streamlit OIDC;
- existing-item prefilling from CSV, including optional BOM component costs;
- new and revised product costings with required-field checks;
- material, production, tooling and per-pallet transport calculations;
- margin-led or selling-price-led pricing;
- append-only revision history showing who created each costing and when;
- customer quote PDFs, filtered audit PDFs and CSV extracts;
- an indicative new-stock-item CSV ready to map to the agreed Sage 200 template.

## Run locally

Use Python 3.12 to match Streamlit Community Cloud.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

The repository starts in clearly labelled demo mode. To require a login:

1. Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`.
2. Run `python scripts/generate_password_hash.py`.
3. Paste the generated hash into your local secrets file.
4. Restart Streamlit.

The real secrets file is excluded from Git.

## Data feeds

| File | Purpose |
| --- | --- |
| `data/current_items.csv` | Existing items and default specification/cost fields |
| `data/bom_costs.csv` | Optional component rows; grouped into a cost per 1,000 |
| `data/saved_costings.csv` | Append-only audit and revision history |

Set `COSTING_DATA_DIR` to keep the live data outside the Git checkout or on a
persistent mounted folder.

The included rows are fictional demonstrations. Replace the files with agreed
exports, retaining the headings, before using the app with live figures.

## Calculation basis in this MVP

Material weight per 1,000 is:

`blank length (m) × blank width (m) × GSM`

The app applies waste, multiplies gross kg by the cost per tonne, then adds BOM,
print, conversion, packing, tooling allocation and transport. Transport is
currently `ceiling(order quantity / pallet quantity) × rate per pallet` and is
then converted to a cost per 1,000. Margin pricing uses:

`selling price = total cost / (1 - margin)`

Keep these rules under review: whether dimensions are blank or net dimensions,
how multi-up production is represented, minimum transport charges, machine and
labour rates, and where print should sit in a multi-level BOM all need to be
agreed before production use.

### Important persistence limitation

GitHub stores the code, not live multi-user transactions. Streamlit Community
Cloud's local runtime storage should not be treated as the permanent audit
record. The CSV repository is suitable for local trials or for a self-hosted app
whose `COSTING_DATA_DIR` points to durable storage. Before a multi-user rollout,
replace the CSV repository with SQL/Postgres, Azure storage or another shared,
backed-up store. The calculation and UI modules are already separated from the
repository to make that change contained.

## Test

```bash
pytest -q
```

The tests cover the cost formula, margin/price behaviour, password hashing and
append-only revision history.

