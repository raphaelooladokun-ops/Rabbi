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
from core.config import TRADER_INVOICE_NUMBER_MAX
from core.engine import ProcessResult, process
from core.masters import ClientConfig, ItemEntry, MasterStore, PartyEntry
from core.models import FlagCode
from core.output import write_csv_bytes
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
def _apply_overrides(result: ProcessResult) -> ProcessResult:
    """Apply operator-supplied manual invoice-number overrides for invoices
    whose number overflowed and could not be auto-trimmed safely."""
    overrides: dict = st.session_state.get("inv_number_overrides", {})
    if not overrides:
        return result
    taken = {iv.trader_invoice_number for iv in result.invoices if iv.trader_invoice_number}
    for iv in result.invoices:
        ov = overrides.get(iv.invoice_number_raw)
        if not ov:
            continue
        ov = ov.strip()
        if len(ov) > TRADER_INVOICE_NUMBER_MAX or ov in taken:
            continue  # invalid/duplicate override is ignored
        iv.trader_invoice_number = ov
        iv.flags = [f for f in iv.flags if f.code != FlagCode.INVOICE_NUMBER_OVERFLOW]
        taken.add(ov)
    return result


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
        st.session_state["inv_number_overrides"] = {}

    for err in st.session_state.get("file_errors", []):
        st.error(err)

    rows = st.session_state.get("rows", [])
    if not rows:
        st.warning("No item lines were found in this file.")
        return

    result = _apply_overrides(process(rows, client, store))

    ready = result.ready_invoices
    blocking = result.blocking_invoices
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Invoices", len(result.invoices))
    c2.metric("Ready", len(ready))
    c3.metric("Need attention", len(blocking))
    c4.metric("Item lines", len(rows))

    _render_exceptions(store, client, result)
    st.divider()
    _render_ready(ready)
    st.divider()
    _render_download(result)


def _render_exceptions(store: MasterStore, client: ClientConfig, result: ProcessResult) -> None:
    st.subheader("Items needing attention")
    flagged = result.flagged_invoices
    if not flagged:
        st.success("No exceptions. Every invoice is ready to export.")
        return

    tax_categories = list(store.tax_rates().keys())
    for idx, iv in enumerate(flagged):
        errors = [f for f in iv.all_flags if f.is_error]
        warns = [f for f in iv.all_flags if not f.is_error]
        icon = "🛑" if errors else "⚠️"
        label = f"{icon} {iv.invoice_number_raw}"
        if iv.branch:
            label += f"  ·  {iv.branch}"
        label += f"  ·  {iv.customer_name}"
        with st.expander(label, expanded=bool(errors)):
            # One invoice can flag the same unknown item/customer on several
            # lines; show each distinct exception only once.
            for j, flag in enumerate(_dedupe_flags(errors + warns)):
                _render_flag(store, client, iv, flag, tax_categories, uid=f"{idx}_{j}")


def _flag_signature(flag) -> tuple:
    """Identity used to collapse repeated flags within one invoice."""
    ctx = flag.context
    return (flag.code, ctx.get("item_name", ""), ctx.get("customer_name", ""), flag.field or "")


def _dedupe_flags(flags):
    seen, out = set(), []
    for f in flags:
        sig = _flag_signature(f)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(f)
    return out


def _render_flag(store, client, iv, flag, tax_categories, uid: str) -> None:
    sev = "🛑 Error" if flag.is_error else "⚠️ Warning"
    st.markdown(f"**{sev} — {flag.code.value}**: {flag.message}")

    if flag.code == FlagCode.ITEM_NOT_FOUND:
        name = flag.context.get("item_name", "")
        with st.form(f"item_{uid}"):
            st.write(f"Add **{name}** to the items master for {client.name}:")
            code = st.text_input("item_code (ITM_xxx)", key=f"code_{uid}")
            hsn = st.text_input("HSN code", value=flag.context.get("hsn", ""), key=f"hsn_{uid}")
            cat = st.selectbox("Tax category", tax_categories, key=f"cat_{uid}")
            if st.form_submit_button("Add item") and code:
                store.upsert_item(client.id, ItemEntry(name=name, item_code=code, hsn_code=hsn, tax_category=cat))
                st.rerun()

    elif flag.code == FlagCode.CUSTOMER_NOT_FOUND:
        name = flag.context.get("customer_name", "")
        with st.form(f"party_{uid}"):
            st.write(f"Add **{name}** to the parties master for {client.name}:")
            status = st.selectbox("Status", ["B2B", "B2C"], key=f"st_{uid}")
            tin = st.text_input("TIN (required for B2B)", key=f"tin_{uid}")
            if st.form_submit_button("Add customer"):
                store.upsert_party(client.id, PartyEntry(name=name, tin=tin, status=status))
                st.rerun()

    elif flag.code == FlagCode.INVOICE_NUMBER_OVERFLOW:
        with st.form(f"ovr_{uid}"):
            st.write(
                f"Provide a unique replacement invoice number (≤ {TRADER_INVOICE_NUMBER_MAX} chars). "
                f"Auto-trim was unsafe: {flag.context.get('attempted', '')}"
            )
            new = st.text_input("trader_invoice_number", value=iv.invoice_number_raw[:TRADER_INVOICE_NUMBER_MAX],
                                key=f"ovrnum_{uid}")
            if st.form_submit_button("Use this number"):
                st.session_state.setdefault("inv_number_overrides", {})[iv.invoice_number_raw] = new
                st.rerun()

    else:
        # Mismatch / TIN format / broken value / length: informational. The
        # operator fixes the source file or the master, then re-uploads.
        st.caption("Resolve at source (fix the file or the relevant master), then re-upload.")


def _render_ready(ready) -> None:
    st.subheader(f"Ready invoices ({len(ready)})")
    if not ready:
        st.caption("None yet.")
        return
    table = [
        {
            "Invoice #": iv.trader_invoice_number,
            "Branch": iv.branch or "",
            "Date": str(iv.invoice_date or ""),
            "Customer": iv.customer_name,
            "Kind": iv.invoice_kind,
            "TIN": iv.party_tin or "",
            "Lines": len(iv.lines),
            "Ex-VAT total": float(iv.ex_vat_total),
        }
        for iv in ready
    ]
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)


def _render_download(result: ProcessResult) -> None:
    st.subheader("Download Digitax CSV")
    blocking = result.blocking_invoices
    if blocking:
        st.warning(
            f"{len(blocking)} invoice(s) still have blocking errors and will be excluded. "
            "Resolve them above, or download only the ready invoices."
        )
    ready = result.ready_invoices
    if not ready:
        st.info("No ready invoices to export yet.")
        return
    csv_bytes = write_csv_bytes(result.invoices, only_ready=True)
    st.download_button(
        f"Download CSV ({len(ready)} invoices)",
        data=csv_bytes,
        file_name="digitax_upload.csv",
        mime="text/csv",
        type="primary",
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
    "name": ["name", "item_name", "item name", "description", "item description"],
    "item_code": ["item_code", "itemcode", "code"],
    "hsn_code": ["hsn_code", "hsn", "hsn_.30", "hsn .30", "hsn30", "hs/service code", "hs_code"],
    "tax_category": ["tax_category", "tax_category_code", "category", "tax category"],
}
_PARTY_ALIASES = {
    "name": ["name", "customer_name", "customer name", "customer", "party"],
    "tin": ["tin", "tin no", "tin_no", "tinno"],
    "status": ["status", "b2b/b2c", "type", "b2b_b2c"],
}


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
                    entries.append(PartyEntry(name=name, tin=tin, status=status))
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
                    "Reader", ["geeta", "friendship", "bag"],
                    index=["geeta", "friendship", "bag"].index(c.reader),
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
            reader = st.selectbox("Reader", ["geeta", "friendship", "bag"])
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
