# Costing Tool

A GitHub-ready Streamlit costing application using the supplied Sage item/BOM
test extracts and haulier price matrix.

## Included workflow

- secure sign-in using local hashed passwords or Streamlit OIDC;
- 354 current items with their Sage analysis values;
- 2,330 detailed BOM lines covering 179 costed items;
- imported BOM materials, print, die-cut, fold-glue, other-machine and labour totals;
- required-field checks before a costing can progress;
- postcode, service, haulier and pallet-count transport pricing;
- automatic comparison of Joda and McDowells where both rates are available;
- margin-led or selling-price-led pricing;
- append-only revision history showing who created each costing and when;
- customer quotation PDFs, audit PDFs and CSV extracts;
- an indicative Sage new-stock-item CSV using the supplied import headings.

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
3. Paste the generated hash into the local secrets file.
4. Restart Streamlit.

The real secrets file is excluded from Git.

## Data feeds

| File | Purpose |
| --- | --- |
| `data/current_items.csv` | Converted item fields and Sage analysis values |
| `data/bom_costs.csv` | Detailed BOM material, labour and machine lines |
| `data/haulier_rates.csv` | Postcode/service/vendor matrix for 1–26 pallets |
| `data/saved_costings.csv` | Append-only costing and revision history |

Set `COSTING_DATA_DIR` to keep live data outside the Git checkout or on a
persistent mounted folder.

## Cost calculation

For items with an imported BOM, the initial production cost is taken from the
supplied BOM totals. The extract reconciles exactly as:

`BOM materials + BOM machine total + BOM labour = BOM total unit cost`

The app exposes the supplied machine breakdown for print, die cutting, fold
gluing and other machinery. Users can amend these values, add a manual cost
adjustment, and allocate fixed tooling across the selected order quantity.

Transport is converted into a cost per 1,000 and added to the manufacturing
cost. Margin pricing uses:

`selling price = total cost / (1 - margin)`

## Transport rules

- The full supplied postcode matrix is used for Economy and Next Day services.
- Both Joda and McDowells are calculated where the workbook contains a rate.
- `Cheapest available` selects the lowest valid total.
- Orders above 26 pallets are split into additional loads.
- AM/PM bookings add £7 per load.
- Timed bookings add £19 per load.
- McDowells adds £40 for each complete 26-pallet load.
- Missing workbook rates are treated as unavailable, not as zero.
- A manual transport total is available for exceptional or unlisted movements.

## Deploy from GitHub

1. Keep `app.py`, `README.md` and `requirements.txt` in the repository root.
2. Preserve the `src`, `data`, `tests`, `scripts` and `.streamlit` folders.
3. Create a private GitHub repository and push the project.
4. In Streamlit Community Cloud, select `app.py` as the entrypoint.
5. Paste production secrets into Advanced settings; never commit them.
6. For company use, prefer Microsoft Entra OIDC and restrict access by domain or email.

### Persistence limitation

GitHub stores the code and input templates, not live multi-user transactions.
The CSV history is suitable for local trials or a self-hosted instance whose
`COSTING_DATA_DIR` points to durable storage. Before multi-user rollout, replace
the CSV history repository with SQL/Postgres, Azure storage or another shared,
backed-up store.

## Items still requiring business confirmation

- Whether AM/PM, timed and McDowells full-load surcharges are current and VAT/FSC treatment.
- How to handle dual collections and special delivery instructions.
- Which users may alter imported BOM values versus only viewing them.
- The mandatory accounts/defaults needed for a fully importable new Sage item row.
- Approval thresholds for margin, manual adjustments and transport overrides.

## Test

```bash
pytest -q
```

The tests cover imported BOM costing, pricing, password hashing, append-only
history, postcode-zone selection, haulier comparisons, full-load rules,
unavailable rates, exports and the Streamlit workflow.

