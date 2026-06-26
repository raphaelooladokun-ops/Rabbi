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
  item line. Tax rate is derived from each item's tax category in the master
  (never hardcoded). Customer → TIN via the parties master.
- **Reader B — Friendship Co** (`friendship`): "SALES LEDGER" `.xlsx`, flat
  (one row per item line). TIN and VAT rate come straight from the file
  (`#N/A` TIN → treated as B2C and flagged). Items resolve by description or
  HSN.
- **Reader C — Bag client** (`bag`): Tally "Sales Register" `.xls`. Reuses the
  parent/child logic. The branch is parsed from the voucher type
  (`SALES INVOICE (KETU)` → `KETU`) and is part of the invoice key, because
  branches run **separate** invoice sequences. The Voucher No. is used exactly
  as written — never renumbered. B2C unless a customer is found as B2B in a
  parties master.

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
`core/config.py: DIGITAX_COLUMNS`). `invoice_type_code = 388`,
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

Masters live as JSON under `data/clients/` and must persist between runs, so any
deployment needs a **persistent volume** mounted there. **Confirm the hosting
preference with the operator before deploying.** Real client data
(`data/clients/<id>/`, `registry.json`) is git-ignored and never committed; only
the seed templates under `data/seed/` are tracked.

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
- Whether the Bag client has any B2B customers.
- Hosting target and the persistent-storage arrangement.
