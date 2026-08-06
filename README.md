# Solidus Costing Tool

This is a Streamlit app for building consistent packaging costings from the
Solidus item, BOM, board-price and haulier data supplied with the project.

The aim is simple: choose or describe a product, add the commercial order
details, let the app calculate material and delivery, then test the effect of
spread on selling price. Machine and labour are deliberately not included.

## What a user does

1. Choose an existing item or start a new one.
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
7. Save a new revision and download a customer quotation, costing CSV or
   indicative Sage stock-item import.

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

The operational spread indicator is separate from pricing:

`spread per machine hour = total cash spread ÷ quoted BOM machine hours`

It includes the available top-level operation speeds and rolled-child machine
time. If a new item has no comparable BOM, the indicator remains unavailable
rather than asking the user to guess a production time.

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

The app is locked until at least one password user is configured. For a basic
internal setup, the chosen password can be stored directly in Streamlit's
private Secrets. It must never be committed to GitHub.

For local use:

1. Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`.
2. Replace the example username, name, email and password.
3. Restart Streamlit.

For Streamlit Community Cloud, open the app's **Settings > Secrets**, paste the
same TOML content there, replace the example name/email/password, and save. The
key after `[users.` is the username used on the login screen. Add another
`[users.username]` block for each authorised user.

User access is controlled in the same block:

```toml
[users.productcreator]
name = "Product Creator"
email = "creator@example.com"
password = "REPLACE_ME"
can_create_new = true

[users.standarduser]
name = "Standard User"
email = "standard@example.com"
password = "REPLACE_ME"
can_create_new = false
```

The new-product permission defaults to `false`. Standard users see only the
existing-product route. Product creators can also create new products and
receive the draft Sage item export. Every user has a private **My costings**
view containing only revisions saved under their own login email; these can be
loaded, amended, recalculated and saved as the next revision.

For stronger password storage, remove the plain `password` entry and use
`python scripts/generate_password_hash.py` to create a `password_hash` instead.
If both are supplied, the hash takes precedence.

The real secrets file is ignored by Git and must never be committed. Explicit
`mode = "demo"` remains available for local development tests only.

## Keeping the data current

The `data` folder contains the app-ready feeds:

| File | Used for |
| --- | --- |
| `current_items.csv` | Existing product and stock information |
| `board_items.csv` | Board dimensions, GSM, FSC and resolved price match |
| `board_prices.csv` | April 2026 mill price rows and aliases |
| `bom_costs.csv` | Material BOM lines and imported audit values |
| `haulier_rates.csv` | Postcode, service, vendor and pallet rates |
| `saved_costings.csv` | Append-only costing revisions |

After replacing the source workbooks, rebuild the app feeds with:

```bash
python scripts/import_workbooks.py \
  --costing-workbook "Costing app test data.xlsx" \
  --board-prices "Mill Price List Comparison Apr 26.XLSX" \
  --output-dir data
```

Set `COSTING_DATA_DIR` if live data and saved costings should sit outside the Git
checkout or on a persistent mounted folder.

## Multiple users

Streamlit keeps each signed-in user's working form in a separate session. CSV
saves are protected by a shared file lock, assigned unique costing and quotation
references, and written atomically, so simultaneous saves on one running app do
not overwrite one another.

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
overrides, approval thresholds, the treatment of unmatched board prices and the
exact Sage 200 import mapping. The prototype Sage CSV is intentionally labelled
indicative until that mapping is signed off.
