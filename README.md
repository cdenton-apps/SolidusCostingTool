# Solidus Costing Tool

This is the working Streamlit costing app for Solidus. It uses the supplied
stock list, BOMs, board prices and haulier rates.

Choose a product, add the customer and order details, and the app works out
material and delivery. You can then change the spread or selling price and see
the other figure update. Machine and labour are not added to the price.

## What a user does

1. Choose an existing BOM-costed item or, if your login allows it, start a new
   one. Products without a costing BOM are left out of the picker for now.
2. Enter the customer, delivery postcode and order quantity. Quantity can be
   entered as units or pallets.
3. Choose the fulfilment type:
   - **MTO (Make to Order):** the order is treated as one delivery event.
   - **MTC (Make to Contract):** enter the agreement term, pallets per call-off
     and any potential pallet holding charge.
4. Review the automatic material calculation and delivery quote.
5. Change either spread percentage or selling price; the other value updates
   immediately.
6. Review spread per machine hour, calculated from the BOM operation speeds
   without adding machine or labour to the pricing base.
7. Save a new revision and download a customer quotation, costing CSV or Sage
   stock-item import row.

For an existing item, the technical specification stays collapsed by default.
It is still available through **View or amend product specification** when a
change is needed.

## Where the numbers come from

- Board is matched to the April 2026 mill price list by article/board code where
  possible. Size and GSM are only used when they produce one unambiguous match.
- Other components such as pallets, banding, layercards, wrap, topsheets and
  adhesive come from the BOM.
- If board cannot be matched safely, the app uses the BOM material value after
  explicitly removing rolled machine and labour values.
- New items use a selected priced board, units out per sheet and an optional
  comparable BOM for the other components. There is no normal free-typed
  material-cost field.
- Delivery is quoted from the supplied Joda and McDowells matrix. For MTC, each
  planned pallet call-off is costed, so ten one-pallet deliveries cost
  differently from one ten-pallet delivery.

The commercial pricing base is material plus any approved commercial
adjustment and delivery pass-through. Spread is a gross
percentage of selling price:

`spread % = (selling price − pricing base) ÷ selling price × 100`

`selling price = pricing base ÷ (1 − spread %)`

The operational spread indicator is separate from customer pricing. It applies
the selected spread percentage to material cost only, so transport distance and
commercial adjustments cannot inflate the operational result:

`spread per machine hour = material-only spread ÷ quoted BOM machine hours`

It includes the available top-level operation speeds and rolled-child machine
time. If a new item has no comparable BOM, the indicator remains unavailable
rather than asking the user to guess a production time.

The pricing page includes an expandable machine-hours calculation. It shows
each operation's run hours, system quantity, effective quantity, quantity source
and calculated hours per 1,000, followed by the total for the quoted quantity.

## Run it locally

Python 3.12 is recommended to match Streamlit Community Cloud.

The Streamlit and Starlette versions are deliberately pinned in
`requirements.txt`. Keep those pins together: newer Starlette releases can
change the server middleware interface before Streamlit has adopted it.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

The app will not open until at least one user has been added. For a basic
internal setup, you can put the chosen password directly in Streamlit Secrets.
Do not put the real password in GitHub.

For local use:

1. Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`.
2. Replace the example username, name, email and password.
3. Restart Streamlit.

For Streamlit Community Cloud, open the app's **Settings > Secrets**, paste the
same TOML content there, replace the example name/email/password, and save. The
key after `[users.` is the username used on the login screen. Add another
`[users.username]` block for each person who needs access.

User access is controlled in the same block:

```toml
[users.productcreator]
name = "Product Creator"
email = "creator@example.com"
password = "REPLACE_ME"
can_create_new = true
can_view_history = true

[users.standarduser]
name = "Standard User"
email = "standard@example.com"
password = "REPLACE_ME"
can_create_new = false
can_view_history = false
```

Both permissions default to `false`. `can_create_new` adds the new-product
route and Sage import download. `can_view_history` adds a read-only **Team
history** page, including the login username and name saved against each
costing. Everyone still has **My costings**, which only shows their own work and
lets them reopen it.

For stronger password storage, remove the plain `password` entry and use
`python scripts/generate_password_hash.py` to create a `password_hash` instead.
If both are supplied, the hash takes precedence.

The real secrets file is ignored by Git and must never be committed. Explicit
`mode = "demo"` remains available for local development tests only.

## Keeping the data current

The `data` folder contains the app-ready feeds:

| File | Used for |
| --- | --- |
| `current_items.csv` | 1,305 BOX items from the latest stock export, including the 581 with costing BOMs |
| `board_items.csv` | Board dimensions, GSM, FSC and resolved price match |
| `board_prices.csv` | April 2026 mill price rows and aliases |
| `bom_costs.csv` | Material BOM lines and imported audit values |
| `material_summaries.csv` | Precalculated material and machine-time summary for quick page loading |
| `haulier_rates.csv` | Postcode, service, vendor and pallet rates |
| `saved_costings.csv` | Append-only costing revisions |

After replacing the source workbooks, rebuild the app feeds with:

```bash
python scripts/import_workbooks.py \
  --costing-workbook "Costing app test data.xlsx" \
  --bom-workbook "Costed BOMs 06.08.xlsx" \
  --stock-csv "stock export 19.06.csv" \
  --board-prices "Mill Price List Comparison Apr 26.XLSX" \
  --output-dir data
```

The stock CSV is the same 72-column layout used by the Sage stock export and
import. If `--stock-csv` is left out, the script uses the `Stock Item Info`
sheet in the costing workbook instead. Items without a supplied costing BOM
remain in the source feed but are hidden from the app until a usable costing is
available.

The full BOM export currently gives the app costings for 581 of the 1,305 BOX
stock items. If `--bom-workbook` is left out, the script uses the `BOM Info`
sheet in the costing workbook.

Machine time uses `EffectiveQuantityPerRun` (column Q in the supplied BOM
export) whenever it is present and positive. `SystemQuantityPerRun` is only the
fallback when the effective quantity is blank.

Set `COSTING_DATA_DIR` if live data and saved costings should sit outside the Git
checkout or on a persistent mounted folder.

## Multiple users

Each signed-in user gets their own working form. Saves use a shared file lock
and unique references, so two people saving at the same time on one running app
do not overwrite one another.

The Streamlit Community Cloud filesystem is not permanent storage. It can be
replaced when the app reboots or redeploys. Before relying on this as the live
costing history for multiple users, move saved revisions to shared persistent
storage such as the company's SQL platform. The current repository layer keeps
that database migration contained to one part of the app.

## Transport behaviour

- Economy and Next Day use the supplied postcode matrix.
- `Cheapest available` compares Joda and McDowells wherever both have a rate.
- A delivery above 26 pallets is split into additional vehicle loads.
- An MTC agreement is split into the planned pallet call-offs before rates are
  added together.
- AM/PM adds £7 per load; Timed adds £19 per load.
- McDowells adds £40 for each complete 26-pallet load.
- Missing rates are unavailable, never treated as zero.
- A manual total remains available for exceptional or unlisted movements.

The customer quotation uses the official Solidus brand artwork and includes a
wrapping product description, key technical specifications, price, delivery
basis, postcode, booking window and call-off profile. Haulier and service detail
is kept in the commercial terms. For MTC, any holding rate entered is shown as
£ per pallet per week; otherwise the paperwork says a rate may be agreed in the
final contract.

Per-item prices retain the decimal places needed for sub-penny pricing. The
notes section is always included, and the supplied Solidus General Terms and
Conditions of Sale and Delivery are appended behind every quotation and
referenced in its commercial terms.

The included quotation artwork is the official Solidus brand header published
with the company's 2023 brand identity announcement.

## GitHub and deployment

Keeping the deployable version on `main` is normal. For day-to-day changes, use
a short-lived branch and pull request, then merge the tested change into
`main`. A practical repository layout is already in place: the entrypoint and
README are at the root, while source code, data, scripts and tests are grouped
in their own folders.

For Streamlit Community Cloud, connect the repository and choose `app.py` as the
entrypoint. Add production secrets in Streamlit's Advanced settings.

GitHub stores code and reference data, not reliable live multi-user
transactions. The CSV history is suitable for trials or a self-hosted instance
with durable storage. Before wider rollout, move costing history to a shared,
backed-up database or storage service.

## Tests

```bash
pytest -q
```

The tests cover board/BOM integration, machine and labour exclusion, percentage
spread, pallet call-offs, haulier rules, authentication, revision history,
exports and the Streamlit workflow.

## Before production use

The business should still confirm surcharge/VAT treatment, permissions for
overrides, approval thresholds, the treatment of unmatched board prices and
the Sage account defaults for each manufacturing site. The Sage download now
uses the exact 72-column layout in the supplied export/import file, but it
should still be checked before importing live records.
