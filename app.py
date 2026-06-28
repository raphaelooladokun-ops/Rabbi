"""Rabbi Consult — e-Invoicing Converter (Streamlit operator app).

One run: pick client -> upload raw sales file -> process -> review & resolve
exceptions -> download the Digitax CSV. No code or config editing during
normal use; masters are maintained through the Masters tab and persist
between runs.
"""
from __future__ import annotations

import io
from decimal import Decimal

import pandas as pd
import streamlit as st

from core.bootstrap import ensure_default_clients
from core.engine import ProcessResult, process
from core.masters import ClientConfig, ItemEntry, MasterStore, PartyEntry
from core.models import FlagCode
from core.output import (
    build_exception_rows,
    write_csv_bytes,
    write_exceptions_csv,
    write_items_template,
    write_parties_template,
)
from core.digitax_resources import TAX_CATEGORY_CODES
from core.proposals import propose_items, propose_party, run_period
from core.readers import get_reader
from core.store import get_master_store
from ui.auth import login_gate, logout_button

st.set_page_config(page_title="Rabbi e-Invoicing Converter", page_icon="🧾", layout="wide")


@st.cache_resource
def get_store():
    store = get_master_store()
    ensure_default_clients(store)
    return store


# ---------------------------------------------------------------------------
# Convert page
# ---------------------------------------------------------------------------
def render_convert(store: MasterStore, client: ClientConfig) -> None:
    st.header(f"Convert — {client.name}")
    st.caption(client.notes)

    uploaded = st.file_uploader(
        "Upload the client's raw sales export",
        type=["xlsx", "xls"],
        key=f"upload_{client.id}",
    )
    if uploaded is None:
        st.info("Upload a file to begin.")
        return

    # Read the raw file once per upload; re-process every render so master
    # edits made during review take effect immediately.
    cache_key = f"{client.id}:{uploaded.name}:{uploaded.size}"
    if st.session_state.get("rows_key") != cache_key:
        try:
            read_result = get_reader(client.reader).read(uploaded.getvalue())
        except Exception as exc:  # noqa: BLE001 - surface any reader failure
            st.error(f"Could not read the file: {exc}")
            return
        st.session_state["rows"] = read_result.rows
        st.session_state["file_errors"] = read_result.file_errors
        st.session_state["rows_key"] = cache_key

    for err in st.session_state.get("file_errors", []):
        st.error(err)

    rows = st.session_state.get("rows", [])
    if not rows:
        st.warning("No item lines were found in this file.")
        return

    result = process(rows, client, store)

    ready = result.ready_invoices
    blocking = result.blocking_invoices
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Invoices", len(result.invoices))
    c2.metric("Ready", len(ready))
    c3.metric("Need attention", len(blocking))
    c4.metric("Item lines", len(rows))

    _render_exceptions(result)
    st.divider()
    _render_create_masters(store, client, result)
    st.divider()
    _render_staged_output(client, result)


# -- per-run session buckets (scoped to the uploaded file) ------------------
def _run_key() -> str:
    return st.session_state.get("rows_key", "")


def _bucket(name: str) -> dict:
    """A dict scoped to the current uploaded file (resets on a new upload)."""
    return st.session_state.setdefault(name, {}).setdefault(_run_key(), {})


def _uploaded_done() -> bool:
    return st.session_state.setdefault("uploaded_done", {}).get(_run_key(), False)


def _distinct_flagged(result: ProcessResult, code: FlagCode, key: str) -> list[str]:
    return sorted({
        f.context.get(key, "")
        for iv in result.invoices for f in iv.all_flags
        if f.code == code and f.context.get(key)
    })


def _render_exceptions(result: ProcessResult) -> None:
    st.subheader("Lines needing attention")
    report = build_exception_rows(result)
    if not report:
        st.success("Nothing flagged — every invoice is ready.")
        return
    st.caption(
        "Resolve these below (Create missing masters), or fix them directly in the downloaded "
        "invoices file — the *source_rows* column points to the row in your original sheet."
    )
    st.dataframe(pd.DataFrame(report), use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download this list (CSV)",
        data=write_exceptions_csv(result).encode("utf-8"),
        file_name="exceptions_report.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# Create missing masters
# ---------------------------------------------------------------------------
def _render_create_masters(store: MasterStore, client: ClientConfig, result: ProcessResult) -> None:
    unknown_items = _distinct_flagged(result, FlagCode.ITEM_NOT_FOUND, "item_name")
    unknown_custs = _distinct_flagged(result, FlagCode.CUSTOMER_NOT_FOUND, "customer_name")
    if not unknown_items and not unknown_custs:
        return

    st.subheader("Create missing masters")
    st.caption(
        "New items are matched against the existing master first to avoid duplicate Digitax "
        "items; only genuinely-new ones get a new code. Nothing is auto-trusted — approve below."
    )
    if unknown_items:
        _render_item_proposals(store, client, unknown_items)
    if unknown_custs:
        _render_party_proposals(store, client, result, unknown_custs)


def _tax_category_options(store: MasterStore) -> list[str]:
    """Official Digitax category codes, plus any extra already in this store."""
    opts = list(TAX_CATEGORY_CODES)
    for c in store.tax_rates().keys():
        if c not in opts:
            opts.append(c)
    return opts


def _render_item_proposals(store: MasterStore, client: ClientConfig, unknown_items: list[str]) -> None:
    items_master = store.list_items(client.id)
    tax_categories = _tax_category_options(store)
    force_new = list(_bucket("not_a_match").keys())
    proposals = propose_items(unknown_items, items_master, force_new=force_new)
    matches = [p for p in proposals if p.kind == "possible_match"]
    new = [p for p in proposals if p.kind == "new"]

    if matches:
        st.markdown(f"**Possible duplicates ({len(matches)})** — confirm each maps to an existing item.")
        mdf = pd.DataFrame([
            {"item": p.name, "maps_to": p.match_code, "similarity": p.match_score, "confirm": True}
            for p in matches
        ])
        edited = st.data_editor(
            mdf, hide_index=True, use_container_width=True, key=f"match_{_run_key()}",
            disabled=["item", "maps_to", "similarity"],
            column_config={"confirm": st.column_config.CheckboxColumn(help="Tick = same item; untick = actually new")},
        )
        if st.button("Apply mappings", key=f"applymap_{_run_key()}"):
            by_code = {e.item_code: e for e in items_master}
            mapped = forced_new = 0
            for _, r in edited.iterrows():
                name = str(r["item"])
                if bool(r["confirm"]):
                    m = by_code.get(str(r["maps_to"]))
                    if m:
                        store.upsert_item(client.id, ItemEntry(
                            name=name, item_code=m.item_code, hsn_code=m.hsn_code,
                            tax_category=m.tax_category, item_category=m.item_category,
                            description=m.description, is_service=m.is_service))
                        mapped += 1
                else:
                    _bucket("not_a_match")[name] = True
                    forced_new += 1
            st.success(f"Mapped {mapped} item(s) to existing codes; {forced_new} moved to 'new'.")
            st.rerun()

    if new:
        st.markdown(f"**New items ({len(new)})** — review (HSN code required), then approve.")
        ndf = pd.DataFrame([
            {"name": p.name, "item_code": p.item_code, "item_category": p.item_category,
             "hsn_code": p.hsn_code, "description": p.description,
             "tax_category_code": p.tax_category_code, "is_service": p.is_service}
            for p in new
        ])
        edited = st.data_editor(
            ndf, hide_index=True, use_container_width=True, key=f"newitems_{_run_key()}",
            disabled=["name", "item_code"],
            column_config={
                "tax_category_code": st.column_config.SelectboxColumn(options=tax_categories),
                "is_service": st.column_config.CheckboxColumn(),
            },
        )
        if st.button("Approve new items", key=f"approveitems_{_run_key()}"):
            created = _bucket("created_items")
            missing_hsn = 0
            for _, r in edited.iterrows():
                if not str(r.get("hsn_code", "")).strip():
                    missing_hsn += 1
                entry = ItemEntry(
                    name=str(r["name"]), item_code=str(r["item_code"]),
                    hsn_code=str(r.get("hsn_code", "")).strip(),
                    tax_category=str(r.get("tax_category_code", "STANDARD_VAT")).strip() or "STANDARD_VAT",
                    item_category=str(r.get("item_category", "")).strip(),
                    description=str(r.get("description", "")).strip() or str(r["name"]),
                    is_service=bool(r.get("is_service", False)))
                store.upsert_item(client.id, entry)
                created[entry.name] = entry
            msg = f"Approved {len(edited)} new item(s)."
            if missing_hsn:
                msg += f" ⚠️ {missing_hsn} still have a blank HSN code."
            st.success(msg)
            st.rerun()


def _render_party_proposals(store, client, result, unknown_custs) -> None:
    # Collect any TIN / address hint seen near each customer in the raw file.
    hints: dict[str, tuple[str, str]] = {}
    for iv in result.invoices:
        for ln in iv.lines:
            if ln.customer_name and ln.customer_name not in hints:
                hints[ln.customer_name] = (ln.customer_tin_hint, ln.customer_address)

    proposals = [
        propose_party(name, tin_hint=hints.get(name, ("", ""))[0],
                      address_text=hints.get(name, ("", ""))[1])
        for name in unknown_custs
    ]
    st.markdown(f"**Customers ({len(proposals)})** — for B2B, fill **TIN + email + address**; "
                "leave blank to keep B2C (not blocked).")
    st.caption("`local_government` (NG-XX-XXX) needs the Digitax LGA code reference — it is left "
               "editable and never guessed. `state` (NG-XX) is derived from the address when possible.")
    pdf = pd.DataFrame([
        {"name": p.name, "tin": p.tin, "email_address": p.email_address,
         "street_name": p.street_name, "city_name": p.city_name, "postal_zone": p.postal_zone,
         "state": p.state, "local_government": p.local_government, "approve": False}
        for p in proposals
    ])
    edited = st.data_editor(
        pdf, hide_index=True, use_container_width=True, key=f"newparties_{_run_key()}",
        disabled=["name"],
        column_config={"approve": st.column_config.CheckboxColumn(help="Tick to save this customer")},
    )
    if st.button("Approve customers", key=f"approveparties_{_run_key()}"):
        created = _bucket("created_parties")
        n_b2b = n_b2c = 0
        for _, r in edited.iterrows():
            if not bool(r.get("approve", False)):
                continue
            tin = str(r.get("tin", "")).strip()
            email = str(r.get("email_address", "")).strip()
            street = str(r.get("street_name", "")).strip()
            is_b2b = bool(tin and email and street)
            entry = PartyEntry(
                name=str(r["name"]), tin=tin, status="B2B" if is_b2b else "B2C",
                email_address=email, street_name=street,
                city_name=str(r.get("city_name", "")).strip(),
                postal_zone=str(r.get("postal_zone", "")).strip(), country="NGA",
                local_government=str(r.get("local_government", "")).strip(),
                state=str(r.get("state", "")).strip())
            store.upsert_party(client.id, entry)
            if is_b2b:
                created[entry.name] = entry
                n_b2b += 1
            else:
                n_b2c += 1
        st.success(f"Saved {n_b2b} B2B and {n_b2c} B2C customer(s).")
        st.rerun()


# ---------------------------------------------------------------------------
# Staged output (masters first, then invoices)
# ---------------------------------------------------------------------------
def _render_staged_output(client: ClientConfig, result: ProcessResult) -> None:
    st.subheader("Download")
    period = run_period(iv.invoice_date for iv in result.invoices)
    slug = client.id
    created_items = list(_bucket("created_items").values())
    created_parties = list(_bucket("created_parties").values())

    if (created_items or created_parties) and not _uploaded_done():
        st.warning(
            "**Upload these to Digitax first.** New items and customers must exist in Digitax "
            "before it will accept invoices that reference them."
        )
        if created_items:
            st.download_button(
                f"⬇️ {slug}_new_items_{period}.csv  ({len(created_items)} items)",
                data=write_items_template(created_items).encode("utf-8"),
                file_name=f"{slug}_new_items_{period}.csv", mime="text/csv", type="primary",
            )
        if created_parties:
            st.download_button(
                f"⬇️ {slug}_new_parties_{period}.csv  ({len(created_parties)} customers)",
                data=write_parties_template(created_parties).encode("utf-8"),
                file_name=f"{slug}_new_parties_{period}.csv", mime="text/csv", type="primary",
            )
        if st.button("✅ Done — I've uploaded these to Digitax", key=f"done_{_run_key()}"):
            st.session_state.setdefault("uploaded_done", {})[_run_key()] = True
            st.rerun()
        st.info("The invoices file unlocks once you confirm the upload above.")
        return

    if created_items or created_parties:
        st.caption("New items/customers uploaded ✓ — their codes and TINs are already embedded below.")

    ready = result.ready_invoices
    col1, col2 = st.columns(2)
    with col1:
        if ready:
            st.download_button(
                f"⬇️ {slug}_invoices_{period}.csv  ({len(ready)} ready)",
                data=write_csv_bytes(result.invoices, only_ready=True),
                file_name=f"{slug}_invoices_{period}.csv", mime="text/csv", type="primary",
                use_container_width=True,
            )
        else:
            st.caption("No fully-ready invoices yet.")
    with col2:
        st.download_button(
            f"⬇️ All invoices incl. flagged ({len(result.invoices)})",
            data=write_csv_bytes(result.invoices, only_ready=False),
            file_name=f"{slug}_invoices_all_{period}.csv", mime="text/csv",
            use_container_width=True,
        )


# ---------------------------------------------------------------------------
# Masters page
# ---------------------------------------------------------------------------
def _import_table(uploaded) -> pd.DataFrame | None:
    if uploaded is None:
        return None
    name = uploaded.name.lower()
    data = uploaded.getvalue()
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(data), dtype=str).fillna("")
    return pd.read_excel(io.BytesIO(data), dtype=str).fillna("")


def _norm_col(c: str) -> str:
    return "".join(ch for ch in str(c).lower() if ch.isalnum())


def _pick(row, columns_norm, aliases) -> str:
    """Pull a value from a spreadsheet row by any of several column aliases.

    Lets the operator import their existing helper sheets as-is, without
    renaming columns (e.g. 'item_name', 'tax_category_code', 'hsn_.30').
    """
    for alias in aliases:
        col = columns_norm.get(_norm_col(alias))
        if col is not None:
            val = row.get(col, "")
            if str(val).strip():
                return str(val).strip()
    return ""


# Accepted source-column names for each target field (normalised on compare).
_ITEM_ALIASES = {
    "name": ["item_name", "name", "item name"],
    "item_code": ["item_code", "itemcode", "code"],
    "hsn_code": ["hsn_code", "hsn", "hsn_.30", "hsn .30", "hsn30", "hs/service code", "hs_code"],
    "tax_category": ["tax_category_code", "tax_category", "category", "tax category"],
    "item_category": ["item_category", "category_name"],
    "description": ["description", "item description"],
    "is_service": ["is_service", "service"],
}
_PARTY_ALIASES = {
    "name": ["name", "customer_name", "customer name", "customer", "party"],
    "tin": ["tax_identification_number", "tin", "tin no", "tin_no", "tinno"],
    "status": ["status", "b2b/b2c", "type", "b2b_b2c"],
    "email_address": ["email_address", "email"],
    "phone_number": ["phone_number(optional)", "phone_number", "phone"],
    "street_name": ["street_name", "street", "address"],
    "city_name": ["city_name", "city"],
    "postal_zone": ["postal_zone", "postal", "zip"],
    "country": ["country"],
    "local_government": ["local_government", "lga"],
    "state": ["state"],
}


def _truthy(text: str) -> bool:
    return str(text).strip().upper() in {"TRUE", "YES", "1", "Y"}


def render_masters(store: MasterStore, client: ClientConfig) -> None:
    st.header(f"Master data — {client.name}")
    items_tab, parties_tab = st.tabs(["Items master", "Parties master"])

    with items_tab:
        st.caption("Maps each item name/description to a Digitax item_code, HSN and tax category.")
        items = store.list_items(client.id)
        df = pd.DataFrame([e.__dict__ for e in items]) if items else pd.DataFrame(
            columns=["name", "item_code", "hsn_code", "tax_category"]
        )
        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, key=f"items_{client.id}"
        )
        if st.button("Save items master", key=f"save_items_{client.id}"):
            entries = [
                ItemEntry(
                    name=str(r.get("name", "")).strip(),
                    item_code=str(r.get("item_code", "")).strip(),
                    hsn_code=str(r.get("hsn_code", "")).strip(),
                    tax_category=str(r.get("tax_category", "STANDARD_VAT")).strip() or "STANDARD_VAT",
                )
                for _, r in edited.iterrows()
                if str(r.get("name", "")).strip()
            ]
            store.seed_items(client.id, entries)
            st.success(f"Saved {len(entries)} items.")

        with st.expander("Import items from a spreadsheet (seed)"):
            st.caption(
                "Accepts your existing helper sheet. Columns are matched flexibly: "
                "item name (item_name/name/description), item_code, HSN (hsn/hsn_.30), "
                "tax category (tax_category/tax_category_code)."
            )
            up = st.file_uploader("CSV/XLSX", type=["csv", "xlsx"], key=f"imp_items_{client.id}")
            table = _import_table(up)
            if table is not None and st.button("Import items", key=f"do_imp_items_{client.id}"):
                cols = {_norm_col(c): c for c in table.columns}
                entries = []
                for _, r in table.iterrows():
                    name = _pick(r, cols, _ITEM_ALIASES["name"])
                    if not name:
                        continue
                    entries.append(ItemEntry(
                        name=name,
                        item_code=_pick(r, cols, _ITEM_ALIASES["item_code"]),
                        hsn_code=_pick(r, cols, _ITEM_ALIASES["hsn_code"]),
                        tax_category=_pick(r, cols, _ITEM_ALIASES["tax_category"]).upper() or "STANDARD_VAT",
                        item_category=_pick(r, cols, _ITEM_ALIASES["item_category"]),
                        description=_pick(r, cols, _ITEM_ALIASES["description"]),
                        is_service=_truthy(_pick(r, cols, _ITEM_ALIASES["is_service"])),
                    ))
                store.seed_items(client.id, entries)
                missing_code = sum(1 for e in entries if not e.item_code)
                st.success(f"Imported {len(entries)} items."
                           + (f" ⚠️ {missing_code} have no item_code." if missing_code else ""))
                st.rerun()

    with parties_tab:
        st.caption("Maps each customer name to a TIN and B2B/B2C status.")
        parties = store.list_parties(client.id)
        df = pd.DataFrame([e.__dict__ for e in parties]) if parties else pd.DataFrame(
            columns=["name", "tin", "status"]
        )
        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, key=f"parties_{client.id}"
        )
        if st.button("Save parties master", key=f"save_parties_{client.id}"):
            entries = [
                PartyEntry(
                    name=str(r.get("name", "")).strip(),
                    tin=str(r.get("tin", "")).strip(),
                    status=str(r.get("status", "B2C")).strip().upper() or "B2C",
                )
                for _, r in edited.iterrows()
                if str(r.get("name", "")).strip()
            ]
            store.seed_parties(client.id, entries)
            st.success(f"Saved {len(entries)} parties.")

        with st.expander("Import parties from a spreadsheet (seed)"):
            st.caption(
                "Columns matched flexibly: customer name (customer_name/name), "
                "tin (tin/TIN NO), status (B2B/B2C). A row with a TIN but no status "
                "is treated as B2B."
            )
            up = st.file_uploader("CSV/XLSX", type=["csv", "xlsx"], key=f"imp_parties_{client.id}")
            table = _import_table(up)
            if table is not None and st.button("Import parties", key=f"do_imp_parties_{client.id}"):
                cols = {_norm_col(c): c for c in table.columns}
                entries = []
                for _, r in table.iterrows():
                    name = _pick(r, cols, _PARTY_ALIASES["name"])
                    if not name:
                        continue
                    tin = _pick(r, cols, _PARTY_ALIASES["tin"])
                    status = _pick(r, cols, _PARTY_ALIASES["status"]).upper()
                    if not status:
                        status = "B2B" if tin else "B2C"
                    entries.append(PartyEntry(
                        name=name, tin=tin, status=status,
                        email_address=_pick(r, cols, _PARTY_ALIASES["email_address"]),
                        phone_number=_pick(r, cols, _PARTY_ALIASES["phone_number"]),
                        street_name=_pick(r, cols, _PARTY_ALIASES["street_name"]),
                        city_name=_pick(r, cols, _PARTY_ALIASES["city_name"]),
                        postal_zone=_pick(r, cols, _PARTY_ALIASES["postal_zone"]),
                        country=_pick(r, cols, _PARTY_ALIASES["country"]) or "NGA",
                        local_government=_pick(r, cols, _PARTY_ALIASES["local_government"]),
                        state=_pick(r, cols, _PARTY_ALIASES["state"]),
                    ))
                store.seed_parties(client.id, entries)
                st.success(f"Imported {len(entries)} parties.")
                st.rerun()


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
def render_settings(store: MasterStore) -> None:
    st.header("Clients & settings")

    st.subheader("Tax categories → VAT rate")
    rates = store.tax_rates()
    rdf = pd.DataFrame([{"category": k, "rate": str(v)} for k, v in rates.items()])
    edited = st.data_editor(rdf, num_rows="dynamic", use_container_width=True, key="rates")
    if st.button("Save tax rates"):
        new = {}
        for _, r in edited.iterrows():
            cat = str(r.get("category", "")).strip().upper()
            if cat:
                new[cat] = Decimal(str(r.get("rate", "0")) or "0")
        store.save_tax_rates(new)
        st.success("Saved tax rates.")

    st.divider()
    st.subheader("Clients")
    for c in store.list_clients():
        with st.expander(f"{c.name}  ({c.id})"):
            with st.form(f"client_{c.id}"):
                name = st.text_input("Name", value=c.name)
                reader = st.selectbox(
                    "Reader", ["geeta", "friendship", "goldcoin"],
                    index=["geeta", "friendship", "goldcoin"].index(c.reader),
                )
                b2b = st.checkbox("Has registered B2B customers", value=c.b2b_expected)
                tin_rule = st.text_input(
                    "TIN suffix normalisation rule (e.g. -0001; leave blank for none)",
                    value=c.tin_suffix_rule,
                )
                notes = st.text_area("Notes", value=c.notes)
                if st.form_submit_button("Save client"):
                    store.save_client(ClientConfig(
                        id=c.id, name=name, reader=reader, b2b_expected=b2b,
                        tin_suffix_rule=tin_rule, notes=notes,
                    ))
                    st.success("Saved.")
                    st.rerun()

    with st.expander("Add a new client"):
        with st.form("new_client"):
            cid = st.text_input("Client id (lowercase, no spaces)")
            name = st.text_input("Name")
            reader = st.selectbox("Reader", ["geeta", "friendship", "goldcoin"])
            b2b = st.checkbox("Has registered B2B customers")
            if st.form_submit_button("Create client") and cid and name:
                store.save_client(ClientConfig(id=cid, name=name, reader=reader, b2b_expected=b2b))
                st.success(f"Created {name}.")
                st.rerun()


# ---------------------------------------------------------------------------
def main() -> None:
    if not login_gate():
        return
    logout_button()
    store = get_store()

    clients = store.list_clients()
    with st.sidebar:
        st.header("Rabbi Consult")
        page = st.radio("Page", ["Convert", "Master data", "Clients & settings"])
        client = None
        if page in ("Convert", "Master data"):
            options = {c.name: c for c in clients}
            choice = st.selectbox("Client", list(options.keys()))
            client = options[choice]

    if page == "Convert":
        render_convert(store, client)
    elif page == "Master data":
        render_masters(store, client)
    else:
        render_settings(store)


if __name__ == "__main__":
    main()
