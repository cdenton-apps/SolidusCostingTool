# Solidus Spread Costing Tool

A GitHub-ready Streamlit costing application using the supplied Sage item/BOM
test extracts and haulier price matrix.

## Included workflow

- secure sign-in using local hashed passwords or Streamlit OIDC;
- 354 current items with their Sage analysis values;
- 986 board stock items, including board code, dimensions, GSM and FSC data;
- 1,163 April 2026 mill price rows, with 579 board items matched to an unambiguous current rate;
- 2,330 detailed BOM lines covering 179 costed items;
- automatic board pricing plus BOM component roll-up, with no normal material-cost typing;
- required-field checks before a costing can progress;
- postcode, service, haulier and pallet-count transport pricing;
- automatic comparison of Joda and McDowells where both rates are available;
- spread-led or selling-price-led pricing in £ per tonne;
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
| `data/board_items.csv` | Board stock information and resolved April 2026 mill match |
| `data/board_prices.csv` | April 2026 mill price list and article aliases |
| `data/bom_costs.csv` | Source BOM extract; only material rows feed the commercial value |
| `data/haulier_rates.csv` | Postcode/service/vendor matrix for 1–26 pallets |
| `data/saved_costings.csv` | Append-only costing and revision history |

Set `COSTING_DATA_DIR` to keep live data outside the Git checkout or on a
persistent mounted folder.

## Cost calculation

For existing items, the app finds every non-informational board component in the
BOM, joins it to the board stock list and applies the April 2026 mill price. It
matches by article/board code first. Size and GSM are used only when they lead to
one unambiguous price.

If the mill list has no safe match, the app automatically falls back to the
BOM's material-only value. For rolled printed-board children, rolled labour and
machine values are explicitly subtracted before the fallback is used. Other
material components such as pallets, banding, layercards, wrap, topsheets and
adhesive are added from their BOM lines.

For a brand-new item, the user selects a priced board item, enters the number of
finished units out per sheet and selects a comparable BOM for the other
components (or explicitly confirms that none are required). The material value
is then calculated; there is no free-typed material-cost field.

Commercial adjustments and allocated tooling are added to the material base as
pass-throughs. Transport is converted into a value per 1,000 and added as a
delivery pass-through.

Spread is measured in pounds per net tonne:

`spread value per 1,000 = target spread (£/tonne) × net kg per 1,000 ÷ 1,000`

`selling price per 1,000 = pricing base per 1,000 + spread value per 1,000`

Entering a selling price performs the inverse calculation and reports the
achieved spread in £ per tonne.

## Refresh the workbook feeds

After replacing the source workbooks, regenerate the app CSVs with:

```bash
python scripts/import_workbooks.py \
  --costing-workbook "Costing app test data.xlsx" \
  --board-prices "Mill Price List Comparison Apr 26.XLSX" \
  --output-dir data
```

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
- Ownership of board items that still have no unambiguous current mill-price match.
- The mandatory accounts/defaults needed for a fully importable new Sage item row.
- Target spread thresholds, and approval rules for commercial adjustments and transport overrides.

## Test

```bash
pytest -q
```

The tests cover imported material pricing, spread calculations, explicit
machine/labour exclusion, password hashing, append-only
history, postcode-zone selection, haulier comparisons, full-load rules,
unavailable rates, exports and the Streamlit workflow.
