# Solidus Costing Tool

This is the Solidus Streamlit costing app. It uses the current stock list, BOMs,
board prices and haulier rates.

Choose a product, add the customer and order details, and the app works out
material and delivery. You can then change the spread or selling price and see
the other figure update. Machine and labour are not added to the price.

## What a user does

1. Choose an existing BOM-costed item or, if your login allows it, start a new
   one. Products without a costing BOM are not shown in the picker.
2. Enter the customer, delivery postcode and order quantity. Quantity can be
   entered as units or pallets.
3. Choose the fulfilment type:
   - **MTO (Make to Order):** the order is treated as one delivery event.
   - **MTC (Make to Contract):** enter the agreement term and minimum pallets per delivery
     and any potential pallet holding charge.
4. Review the automatic material calculation and delivery quote.
5. Enter the expected annual unit volume and select any relevant customer
   factors. The app chooses the internal volume band automatically.
6. Change either spread percentage or selling price; the other value updates
   immediately.
7. Review spread per machine hour, calculated from the BOM operation speeds
   without adding machine or labour to the pricing base.
8. Save a new revision and download a customer quotation, costing CSV or Sage
   stock-item import row.

Downloads only become available after the current revision has been saved. If
the quotation is changed afterwards, it must be saved as another revision
before any updated PDF or CSV can be downloaded. This keeps the quotation
history aligned with the paperwork issued.

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
- Printed-board BOM components are resolved through their child BOM to the
  underlying plain sheet. Its board grade and price drive the board cost, while
  print-route consumables remain visible as standard-quantity BOM components.
- New items record the complete flat net/blank separately from the finished
  product size. Board fit uses that complete net with a 10 mm edge and
  separation margin, checks both orientations, and automatically costs the
  highest verified x-up. A 1-up result remains visibly inefficient because
  2-up or more is the goal.
- A complete comparable BOM is required for a new item. The selected plain
  board replaces the template board; every other non-board material component
  is retained at its standard BOM quantity. Machine and labour remain outside
  the material pricing base.
- Board material is derived from the board description after the GSM, for
  example `KL/TKL.WPE`; users do not choose a generic material category. If a
  selected or new board has no price, its plain-board price is entered with the
  product details. **Fill board details from code** brings across its GSM, size,
  material and matched mill price where available.
- Delivery is quoted from the Joda and McDowells rate matrix. For MTC, each
  planned pallet call-off is costed, so ten one-pallet deliveries cost
  differently from one ten-pallet delivery.

The commercial pricing base is material plus delivery. Spread is a gross
percentage of selling price:

`spread % = (selling price − pricing base) ÷ selling price × 100`

`selling price = pricing base ÷ (1 − spread %)`

The current annual-volume adjustments are:

| Annual volume | Material adjustment |
| --- | ---: |
| 0–10,000 | +15% |
| 10,001–25,000 | +10% |
| 25,001–50,000 | +5% |
| 50,001–100,000 | 0% |
| 100,001–1,000,000 | −10% |
| Over 1,000,000 | −15% |

The current COMEX placeholders are **Consistent Payer −5%**, **Strategic
Customer −3%**, **Over Credit Limit +10%** and **Poor Payment History +5%**.
All selected percentages are added together and applied once to material cost;
they are not compounded. Delivery remains the haulier price. These internal
factors are saved in costing history but are not printed on the customer quote.
Users enter an annual unit volume rather than choosing from the band table, so
the thresholds and percentages are not displayed in the working form.

Non-admin users have a reduced commercial view. They can enter the spread or
selling price and can see the selling price, spread, spread per machine hour,
machine time and traffic-light result. Material costs, adjustments, delivery
cost, component rates, alternative haulier prices and the full costing CSV are
admin-only. Non-admin history downloads use the same reduced set of figures.

The operational spread indicator is separate from customer pricing. It applies
the selected spread percentage to material cost only, so transport distance
cannot inflate the operational result:

`spread per machine hour = material-only spread ÷ quoted BOM machine hours`

It includes the available top-level operation speeds and rolled-child machine
time. If a new item has no comparable BOM, the indicator remains unavailable
rather than asking the user to guess a production time.

The pricing page includes an expandable machine-hours calculation. It shows
each operation's run hours, system quantity, effective quantity, quantity source
and calculated hours per 1,000, followed by the total for the quoted quantity.
The total is shown as both decimal hours and an hours/minutes duration, with
seconds retained in the expanded audit for easier checking.

The commercial check uses the following rules:

- **Green:** at least £600 spread per machine hour and at least 30% spread.
- **Amber:** at least £600 spread per machine hour and 25% to under 30% spread.
  The app shows a prominent warning and requires the current user to acknowledge
  it before continuing.
- **Red:** below £600 spread per machine hour or below 25% spread.

A red costing is blocked. If an administrator is already signed in, they can
enter a reason and approve it directly. An ordinary user sends the exact red
costing to the Neon approval queue. An administrator can approve or decline it
from **Admin tools** in their own account, and the user can then refresh the
status without sharing a browser or password. The saved revision records the
original red result, reason, approver and approval time.
Changing the price or spread cancels the approval and requires another review.
The high-volume bands use the same recorded admin route when a red result needs
to proceed; no extra discount is applied.

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

The app will not open until at least one user has been added. Streamlit Secrets
provides the first administrator account. Once Neon is connected, that account
can import the configured users and manage later accounts from **User
activity**. Do not put real passwords in GitHub.

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
is_admin = true

[users.standarduser]
name = "Standard User"
email = "standard@example.com"
password = "REPLACE_ME"
can_create_new = false
can_view_history = false
is_admin = false
```

All three permissions default to `false`. `can_create_new` adds the new-product
route and Sage import download. `can_view_history` adds a read-only **Team
history** page, including the login username and name saved against each
costing. Everyone still has **My costings**, which only shows their own work and
lets them reopen it. `is_admin` adds **User activity**, including current
sessions, approximate active time, saved-work totals and forced sign-out.

Set `session_timeout_minutes = 60` under `[app_auth]` to change the inactivity
limit. The minimum is five minutes. Forced sign-out is checked every 30 seconds
for password sessions. For OIDC, list admin accounts in `admin_emails` and use
the identity provider as the main place to revoke account access.

For stronger password storage, remove the plain `password` entry and use
`python scripts/generate_password_hash.py` to create a `password_hash` instead.
If both are supplied, the hash takes precedence.

The real secrets file is ignored by Git and must never be committed. Explicit
`mode = "demo"` remains available for local development tests only.

## Dropbox Sign test workflow

Dropbox Sign is currently available in non-binding test mode only. Add the API
key to Streamlit Secrets; never add it to GitHub:

```toml
[esign]
provider = "dropbox_sign"
api_key = "PASTE_THE_DROPBOX_SIGN_API_KEY_HERE"
test_mode = true
director_name = "Sales Director"
director_email = "director@example.com"
director_absent = false
amber_approver_name = "Amber Commercial Approver"
amber_approver_email = "amber.approver@example.com"
amber_approver_absent = false
```

Both commercial approvers are controlled centrally through Streamlit Secrets.
Quotation users can see the resolved recipient but cannot change it. Amber
normally routes to the amber approver and red to the Director. During an
absence, set that person's `*_absent` value to `true`; the other approver then
covers automatically. Set it back to `false` on their return. If both are marked
absent, the app blocks e-sign sending until the cover settings are corrected.

Each salesperson manages their own signature under **Menu > My signature**.
The signature is stored against that username in Neon and cannot be selected or
changed by another user. Saving a quotation records the exact signature version,
name, date and file digest used for that revision. Replacing or removing a
signature therefore affects new revisions only; an older quotation continues to
use the version that was recorded when it was saved.

For a green quotation, the salesperson's saved signature is already on the PDF
and Dropbox Sign sends the document to the Customer only. For amber, the
configured amber approver signs first; for red, the Sales Director or delegated
individual signs first. The Customer follows after that internal signature.
Only the salesperson who owns the saved revision can send it. Their username,
name, email and approval time remain in the Neon audit record.

The app is hard-coded to send with Dropbox Sign `test_mode=1`, so test documents
are watermarked and are not legally binding. Use **Refresh signing status** to
poll Dropbox Sign; once the required signers have finished, the completed test
PDF can be downloaded from the app. A production route must be designed and
approved separately.

## Neon database storage

Saved costing revisions and user-session activity use Neon when a database URL
is present. Product, BOM, board-price and haulier reference feeds remain as CSV
files in the app package.

1. Open the **Solidus Costing Tool** project in Neon.
2. Choose **Connect**, enable connection pooling, and copy the pooled connection
   string. Its hostname includes `-pooler`.
3. In Streamlit Community Cloud, open **App settings > Secrets**.
4. Add the following above the existing `[app_auth]` and `[users...]` sections:

```toml
[database]
url = "PASTE_THE_POOLED_NEON_CONNECTION_STRING_HERE"
```

5. Save the Secrets. The sidebar will show **Storage: Neon database** after the
   app restarts.

The connection string contains the database password. Keep it in Streamlit
Secrets only; never add it to GitHub. If the `[database]` section is absent, the
app continues to use local CSV storage so local development still works.

Run the current `sql/neon_schema.sql` as the Neon owner when upgrading an
existing database. This creates the versioned personal-signature table as well
as any other missing tables and indexes. The application database role is not
allowed to create this table itself.

After the user tables have been prepared, an administrator should open **User
activity** and select **Import missing users from Streamlit Secrets**. Passwords
are converted to one-way hashes before they are written to Neon, and users
imported from a plain password must replace it at their next login. A
`password_hash` entry is imported only when it is a genuine hash generated by
the supplied password script; ordinary text placed in that field is skipped.
Once at least one user exists in Neon, database users take precedence over the
`[users...]` blocks in Secrets.

The administrator can then create, disable and edit users in the app. The three
main access levels are:

| Access level | What it allows |
| --- | --- |
| External | Existing products and the user's own saved costings |
| Creator | Existing products, new products and the user's own saved costings |
| Administrator | Full access, approvals, team history, users and sessions |

A separate **Can view team history** permission can be added to a non-admin
account. New users receive a temporary password and must choose another one at
their first login. Passwords must contain at least 10 characters. Five failed
password attempts within 15 minutes temporarily lock the account for 15
minutes; an administrator can unlock it sooner from **Admin tools**. Password
changes and administrator resets invalidate every existing session for that
account, so the user must sign in again. Disabling an account also requests
sign-out for its open sessions. Successful and failed sign-ins, temporary
locks, unlocks, user creation, access changes and password resets are retained
in the user audit log.

An administrator can also open **User activity > Import earlier CSV costing
history** to copy revisions still held in `saved_costings.csv`. Existing Neon
records are skipped.

For UK use, create the production project in Neon's **AWS Europe (London)**
region. The database URL currently used by Streamlit can be replaced without
changing the application files.

## Keeping the data current

The `data` folder contains the app-ready feeds:

| File | Used for |
| --- | --- |
| `current_items.csv` | 1,313 BOX items from the 11 August stock export, including the 594 with usable costing BOMs |
| `board_items.csv` | Board dimensions, GSM, FSC and resolved price match |
| `board_prices.csv` | April 2026 mill price rows and aliases |
| `bom_costs.csv.gz` | Compressed material BOM lines and imported audit values |
| `material_summaries.csv` | Precalculated material and machine-time summary for quick page loading |
| `haulier_rates.csv` | Postcode, service, vendor and pallet rates |
| `saved_costings.csv` | Local fallback and optional source for importing older revisions into Neon |

After replacing the source workbooks, rebuild the app feeds with:

```bash
python scripts/import_workbooks.py \
  --costing-workbook "Costing app test data.xlsx" \
  --bom-workbook "BOMs 11.08.xlsx" \
  --stock-csv "stock export 11.08.csv" \
  --board-prices "Mill Price List Comparison Apr 26.XLSX" \
  --output-dir data
```

The stock CSV is the same 72-column layout used by the Sage stock export and
import. If `--stock-csv` is left out, the script uses the `Stock Item Info`
sheet in the costing workbook instead. Items without a costing BOM
remain in the source feed but are hidden from the app until a usable costing is
available.

The full BOM export currently gives the app costings for 594 of the 1,313 BOX
stock items. If `--bom-workbook` is left out, the script uses the `BOM Info`
sheet in the costing workbook.

Machine time uses `EffectiveQuantityPerRun` (column Q in the BOM
export) whenever it is present and positive. `SystemQuantityPerRun` is only the
fallback when the effective quantity is blank.

Set `COSTING_DATA_DIR` if the CSV reference feeds should sit outside the Git
checkout.

## Multiple users

Each signed-in user gets their own working form. Saves use a shared file lock
in local CSV mode. With Neon configured, revision allocation is handled inside
the database, so simultaneous users cannot take the same product revision.

Active time still counts short gaps between user actions rather than the whole
time a browser tab is open. With Neon configured, the session register and
saved revisions survive Streamlit restarts and redeployments. It remains a
practical app-activity record rather than a formal HR monitoring system.

## Transport behaviour

- Economy and Next Day use the postcode matrix in `haulier_rates.csv`.
- `Highest available` is the default and selects the highest complete Joda or
  McDowells quotation. Admin users can still explicitly select either haulier
  or `Cheapest available`.
- A delivery above 26 pallets is split into additional vehicle loads.
- Entering more than 26 pallets requires an explicit “Are you sure?” confirmation.
- An MTC agreement is split into the planned pallet call-offs before rates are
  added together.
- AM/PM adds £7 per load; Timed adds £19 per load.
- McDowells adds £40 for each complete 26-pallet load.
- Missing rates are unavailable, never treated as zero.
- A manual total remains available for exceptional or unlisted movements.

## Tooling behaviour

- Every item defaults to a £1,000 one-off forme / stereo charge.
- The pricing base includes £10 tooling amortisation per 1,000 units.
- If tooling is made FOC, the one-off charge is removed and the pricing-base
  amortisation doubles to £20 per 1,000 units.

The customer quotation uses the official Solidus brand artwork and includes a
wrapping product description, key technical specifications, price, delivery
basis, postcode, booking window and call-off profile. Haulier and service detail
is kept in the commercial terms. For MTC, any holding rate entered is shown as
£ per pallet per week; otherwise the paperwork says a rate may be agreed in the
final contract.

GBP remains the normal quotation currency. EUR can be selected at the pricing
stage. The app retrieves the latest available ECB GBP-to-EUR reference rate
through Frankfurter and caches it for one hour; users do not type the rate.
Material, transport and the spread-per-hour gate remain based on GBP costs;
only the customer quotation values are converted. Delivery is DAP by default.
Tick **Collected** when the customer will collect and delivery should be
excluded. The commercial terms state that prices exclude VAT, payment is due
within 30 days unless agreed otherwise, lead time is confirmed when a valid
purchase order is accepted, and the quotation remains valid for three months.

Per-item prices retain the decimal places needed for sub-penny pricing. The
notes section is always included, and the Solidus General Terms and
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

The business should still confirm surcharge treatment, permissions for
overrides, approval thresholds, the treatment of unmatched board prices and
the Sage account defaults for each manufacturing site. The Sage download now
uses the standard 72-column Sage stock export/import layout, but it
should still be checked before importing live records.
