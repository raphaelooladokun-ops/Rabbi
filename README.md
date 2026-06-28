# Rabbi Consult — e-Invoicing Converter

Converts each client's raw sales export into the **Digitax upload CSV** (which
Digitax forwards to NRS), replacing the manual, invoice-by-invoice keying into
the Digitax portal. **One app serves all clients.** The processing logic and
output format are identical for every client — only the per-client *reader*
that understands each raw file layout differs.

## How a run works (operator)

1. Sign in.
2. Pick the client.
3. Upload their raw sales file (`.xlsx`/`.xls`).
4. The app processes it.
5. **Review & resolve** anything flagged (unknown item/customer, total
   mismatch, over-long invoice number, etc.). Resolving an unknown item or
   customer appends it to that client's master, so future runs recognise it.
6. Download the Digitax CSV (only the invoices with no blocking errors are
   written).

No code or config editing is needed during normal use.

## Architecture

```
raw file ──► per-client Reader ──► common internal table ──► shared Engine ──► Digitax CSV writer
            (layout only)          (list[LineRow])          (resolve/validate/   (exact columns)
                                                             reconcile)
```

Everything after the reader is shared by every client. **Adding a future
client = adding one reader** (`core/readers/`) and registering it — not a new
app.

| Module | Responsibility |
|---|---|
| `core/readers/` | One reader per client; turns a raw file into `LineRow`s. No master lookups, no tax decisions. |
| `core/models.py` | The common internal table (`LineRow`) and exception `Flag`s. |
| `core/masters.py` | Persistent, operator-editable item/party masters, client registry, tax rates (JSON under `data/clients/`). |
| `core/engine.py` | Resolves item codes / TINs, applies the invoice-number rule, reconciles totals, routes exceptions to review. |
| `core/output.py` | Writes the exact Digitax CSV. |
| `app.py` + `ui/` | Streamlit operator UI (login, convert, review, masters, settings). |

### The three readers

- **Reader A — Geeta** (`geeta`): Tally "Sales Register" `.xlsx`. Parent/child
  rows; the invoice's date/voucher no./customer are forward-filled onto each
  item line. Every figure is pre-VAT. Tax rate is derived from each item's tax
  category in the master (never hardcoded). Customer → TIN via the parties
  master.
- **Reader B — Friendship Co** (`friendship`): "SALES LEDGER" `.xlsx`, flat
  (one row per item line). TIN and VAT rate come straight from the file (rate
  from the VAT column: 0.075 / blank = 0; `#N/A` TIN → treated as B2C and
  flagged). `unit_price` is the pre-VAT Base P. Items resolve by description or
  HSN.
- **Reader C — Goldcoin** (`goldcoin`): Tally "Sales Register". Treated exactly
  like Geeta for VAT/totals (pre-VAT throughout, tax from the items master).
  The branch is parsed from the voucher type (`SALES INVOICE (KETU)` → `KETU`)
  and is part of the invoice key, because branches run **separate** invoice
  sequences. The Voucher No. is used exactly as written — never renumbered.
  B2C unless a customer is found as B2B in a parties master.

### VAT & totals (per client)

`unit_price` in the output is **always pre-VAT (VAT-exclusive)**.

- **Geeta / Goldcoin** — every figure in the file is pre-VAT; `unit_price` is
  the rate as-is, tax rate comes from the item's category, and reconciliation
  compares the **pre-VAT line sum against the pre-VAT subtotal** (the net/sales
  figure), not the VAT-inclusive gross.
- **Friendship** — tax rate is taken directly from the file's VAT column;
  `unit_price` is the pre-VAT Base P; reconciliation is pre-VAT on both sides.

Reconciling pre-VAT lines against a VAT-inclusive total was the cause of the
earlier "off by exactly 7.5%" mismatches; a small rounding tolerance is
allowed (`RECONCILE_TOLERANCE`).

## Master data

Each client has two reference lists, maintained by the operator under the
**Master data** tab and persisted between runs:

- **Items master** — item name/description → `item_code` (`ITM_xxx`), HSN code,
  tax category (`STANDARD_VAT`, `EXEMPT`, …).
- **Parties master** — customer name → TIN and B2B/B2C status.

Seed them by importing your existing helper columns/sheets (CSV/XLSX) — see the
templates in `data/seed/`. Resolving an unknown item/customer during review
appends it automatically.

Tax categories map to VAT rates under **Clients & settings** (default
`STANDARD_VAT = 0.075`, `EXEMPT = 0`). The 7.5 % rate is configuration, not a
hardcoded constant.

## Create missing masters (new items & parties)

When a run contains items/customers not yet in the masters, the Convert page
proposes them for approval before anything is written:

- **Items** — each unknown item is **fuzzy-matched against the existing master
  first**. A close match (spacing/punctuation/case/minor wording) becomes a
  *confirm-mapping* suggestion ("maps to ITM_xxx") so Digitax never gets a
  duplicate item. Only genuinely-new items get a **new code**, continuing the
  client's existing `ITM_` sequence in the same width, with category /
  `tax_category_code` / `is_service` drafted from the master's existing
  patterns and **HSN left for the operator to fill**. One physical item gets
  one code per run.
- **Parties** — the output party TIN **always comes from the parties master**,
  for every client. A TIN sitting in the sales file (Friendship's ledger, or a
  Geeta/Goldcoin VAT No.) is treated only as a **hint**: an unknown customer is
  B2C until added to the master. In the customers section each unknown customer
  has its TIN pre-filled from the sales file where present; the operator sets
  the status (B2B needs a real TIN — never fabricated) and completes the
  address via a **State dropdown that filters the LGA dropdown to that state**
  (NG-XX / NG-XX-XXX from the bundled reference, so no scrolling 774 LGAs).
  Customers left without a TIN stay **B2C** and are not blocked.

On approval the new items/parties are appended to the persistent masters and
written to Digitax upload templates. **Staged output** then enforces order:
`<client>_new_items_<period>.csv` and `<client>_new_parties_<period>.csv` are
released first with "Upload these to Digitax first"; after a "Done — I've
uploaded these" confirmation, `<client>_invoices_<period>.csv` is released with
the new codes/TINs already embedded. If there are no new items/parties it skips
straight to the invoices file.

## Key safety rules (never silently guess on a tax filing)

- **Invoice-number 30-char cap** — trimmed from the end, **once per invoice**
  so all its lines share the number. If a trim would collide with another
  invoice in the batch it is **not** applied automatically; it goes to review
  for manual resolution (a silent merge would drop an invoice from the filing).
- **Reconciliation** — each invoice's line totals must match its stated total
  (VAT-inclusive or -exclusive as the source declares); mismatches are flagged.
- **Field lengths** validated against Digitax limits; overflow it can't safely
  fix is flagged.
- Unknown item/customer, broken source values and TIN-format problems all go to
  the review screen rather than being auto-resolved.

## Output — Digitax CSV

One row per item line, with exactly the columns Digitax expects (see
`core/config.py: DIGITAX_COLUMNS`). `invoice_type_code` defaults to `381`
(the Digitax invoice-type reference's code for "Commercial Invoice" — the
reference is the source of truth; editable per client),
`document_currency_code = NGN`, `unit_price` is VAT-exclusive, `tax_rate`
carries the per-line VAT rate, `invoice_kind` is `B2B`/`B2C`. **Validate a
processed run against a known-good Digitax CSV for the same period before going
live.**

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

### Login

Configure operator accounts with the `RABBI_USERS` environment variable (or a
Streamlit secret) — a JSON map of username → SHA-256 password hash:

```bash
export RABBI_USERS='{"subomi": "<sha256-hex>"}'
# hash a password:
python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" 'my-password'
```

If unset, the app falls back to `admin` / `rabbi-change-me` and shows a warning.

### Persistence & hosting

Masters persist between runs in one of two interchangeable backends, chosen
automatically by `core/store.py`:

- **Local files (default)** — JSON under `data/clients/`. Zero setup; ideal on a
  laptop. Real client data is git-ignored; only the seed templates under
  `data/seed/` are tracked.
- **Database** — set `DATABASE_URL` (Streamlit secret or env var) and the same
  data is stored via SQLAlchemy (e.g. a free Postgres). Required on hosts with
  an ephemeral disk such as Streamlit Community Cloud.

**To put it online, follow [`DEPLOY.md`](DEPLOY.md)** — a step-by-step,
non-technical guide (free Streamlit Community Cloud + optional free Neon
Postgres for persistence). Confirm the hosting preference with the operator
before deploying.

## Tests

```bash
pytest
```

Covers each reader, item/party/tax resolution, the invoice-number rule
(including unsafe-trim collisions), reconciliation, and the CSV output format,
using synthetic Tally/SALES-LEDGER fixtures.

## Open items to confirm with the operator

- Real seed files for the item and party masters (existing helper
  columns/sheets) and a few sample raw exports per client to validate against.
- The exact Digitax CSV header/format from a known-good file (the column list
  here follows the brief; confirm field order and date formatting match).
- Friendship TIN normalisation rule (e.g. appending a missing `-0001` suffix).
- Whether the Goldcoin client has any B2B customers.
- Hosting target and the persistent-storage arrangement.
