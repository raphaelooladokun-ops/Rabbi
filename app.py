"""Rabbi Consult — e-Invoicing Converter (Streamlit operator app).

One run: pick client -> upload raw sales file -> process -> review & resolve
exceptions -> download the Digitax CSV. No code or config editing during
normal use; masters are maintained through the Masters tab and persist
between runs.
"""
from __future__ import annotations

import io
import re
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
from core.parsing import looks_like_tin
from core.digitax_resources import TAX_CATEGORY_CODES
from core import reference
from core.proposals import propose_items, propose_party, run_period
from core.reference import invoice_type_label, is_valid_hsn, is_valid_service_code
from core.readers import get_reader
from core.store import get_master_store
from ui.auth import login_gate, logout_button

st.set_page_config(page_title="Rabbi e-Invoicing Converter", page_icon="🧾", layout="wide")

# Run a block as an isolated fragment when available (Streamlit >= 1.33), so a
# widget change inside it re-renders only that block — not the whole app/engine.
_fragment = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
if _fragment is None:  # pragma: no cover - very old Streamlit fallback
    def _fragment(func):
        return func


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
        st.caption("HSN codes are validated against the Digitax HSN reference; unknown ones are flagged but not blocked.")
        if st.button("Approve new items", key=f"approveitems_{_run_key()}"):
            created = _bucket("created_items")
            missing_hsn = bad_hsn = 0
            for _, r in edited.iterrows():
                hsn = str(r.get("hsn_code", "")).strip()
                is_svc = bool(r.get("is_service", False))
                if not hsn:
                    missing_hsn += 1
                elif not (is_valid_service_code(hsn) if is_svc else is_valid_hsn(hsn)):
                    bad_hsn += 1
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
                msg += f" ⚠️ {missing_hsn} have a blank HSN code."
            if bad_hsn:
                msg += f" ⚠️ {bad_hsn} HSN code(s) are not in the Digitax reference."
            st.success(msg)
            st.rerun()


_GRID_RATIOS = [0.5, 3.0, 2.0, 2.6, 2.6, 1.4, 1.4, 2.2, 2.6]
_GRID_HEADERS = ["✓", "Customer", "TIN", "Email", "Street", "City", "Postal", "State", "LGA"]


def _render_party_proposals(store, client, result, unknown_custs) -> None:
    # Collect any TIN / address hint seen near each customer in the raw file.
    hints: dict[str, tuple[str, str]] = {}
    for iv in result.invoices:
        for ln in iv.lines:
            if ln.customer_name and ln.customer_name not in hints:
                hints[ln.customer_name] = (ln.customer_tin_hint, ln.customer_address)

    with_tin = [n for n in unknown_custs if hints.get(n, ("", ""))[0]]
    without_tin = [n for n in unknown_custs if not hints.get(n, ("", ""))[0]]

    st.markdown("**Customers** — Digitax needs the full record (TIN, email, address, state, LGA) "
                "for a B2B party. Pick the **State** and the **LGA** list shows only that state's LGAs. "
                "Nothing is fabricated; a blank TIN saves the customer as B2C.")

    if with_tin:
        st.markdown(f"#### ✅ With a TIN from the sales file ({len(with_tin)}) — settle these as B2B")
        _party_grid("withtin", with_tin, hints, store, client, default_approve=True)

    if without_tin:
        with st.expander(f"Other customers without a TIN ({len(without_tin)}) — B2C unless you add a TIN"):
            _party_grid("notin", without_tin, hints, store, client, default_approve=False)


def _party_grid(group: str, names, hints, store, client, default_approve: bool) -> None:
    """A table-like grid of per-row widgets so each row's LGA dropdown can be
    narrowed to the state selected on that same row.

    Wrapped in a fragment so picking a state re-renders only this grid — the
    engine is NOT re-run on every dropdown change (that was the slowness).
    """

    @_fragment
    def _grid() -> None:
        _party_grid_body(group, names, hints, store, client, default_approve)

    _grid()


def _party_grid_body(group: str, names, hints, store, client, default_approve: bool) -> None:
    state_pairs = reference.states()
    state_opts = [""] + [f"{n} ({c})" for n, c in state_pairs]
    s_label2code = {f"{n} ({c})": c for n, c in state_pairs}
    s_code2label = {c: f"{n} ({c})" for n, c in state_pairs}

    header = st.columns(_GRID_RATIOS)
    for col, h in zip(header, _GRID_HEADERS):
        col.markdown(f"**{h}**")

    for i, name in enumerate(names):
        tin_hint, addr = hints.get(name, ("", ""))
        prop = propose_party(name, tin_hint=tin_hint, address_text=addr)
        k = f"{group}_{_run_key()}_{i}"
        c = st.columns(_GRID_RATIOS)
        c[0].checkbox("a", value=default_approve, key=f"ap_{k}", label_visibility="collapsed")
        c[1].markdown(name)
        c[2].text_input("t", value=prop.tin, key=f"tin_{k}", label_visibility="collapsed")
        c[3].text_input("e", key=f"em_{k}", label_visibility="collapsed", placeholder="email")
        c[4].text_input("s", value=addr, key=f"str_{k}", label_visibility="collapsed", placeholder="street")
        c[5].text_input("ci", key=f"city_{k}", label_visibility="collapsed", placeholder="city")
        c[6].text_input("p", key=f"pz_{k}", label_visibility="collapsed", placeholder="zip")
        # State -> dependent LGA (only this state's LGAs).
        pre_state = s_code2label.get(prop.state, "")
        s_idx = state_opts.index(pre_state) if pre_state in state_opts else 0
        state_label = c[7].selectbox("st", state_opts, index=s_idx, key=f"state_{k}",
                                     label_visibility="collapsed")
        sc = s_label2code.get(state_label, "")
        lga_pairs = reference.lgas_for_state(sc) if sc else []
        lga_opts = [""] + [f"{n} ({code})" for n, code in lga_pairs]
        l_code2label = {code: f"{n} ({code})" for n, code in lga_pairs}
        pre_lga = l_code2label.get(prop.local_government, "")
        l_idx = lga_opts.index(pre_lga) if pre_lga in lga_opts else 0
        c[8].selectbox("lg", lga_opts, index=l_idx, key=f"lga_{k}", label_visibility="collapsed")

    if st.button("Approve these customers", key=f"gridbtn_{group}_{_run_key()}"):
        created = _bucket("created_parties")
        n_b2b = n_b2c = incomplete = junk_tin = 0
        for i, name in enumerate(names):
            k = f"{group}_{_run_key()}_{i}"
            if not st.session_state.get(f"ap_{k}"):
                continue
            tin = re.sub(r"\s+", "", str(st.session_state.get(f"tin_{k}", "")))  # no spaces in a TIN
            if tin and not looks_like_tin(tin):
                junk_tin += 1
                tin = ""
            sc = s_label2code.get(st.session_state.get(f"state_{k}", ""), "")
            lga_pairs = reference.lgas_for_state(sc) if sc else []
            lc = {f"{n} ({code})": code for n, code in lga_pairs}.get(st.session_state.get(f"lga_{k}", ""), "")
            email = str(st.session_state.get(f"em_{k}", "")).strip()
            street = str(st.session_state.get(f"str_{k}", "")).strip()
            status = "B2B" if tin else "B2C"  # never fabricate a TIN
            if status == "B2B" and not (email and street and sc and lc):
                incomplete += 1
            entry = PartyEntry(
                name=name, tin=tin, status=status, email_address=email, street_name=street,
                city_name=str(st.session_state.get(f"city_{k}", "")).strip(),
                postal_zone=str(st.session_state.get(f"pz_{k}", "")).strip(),
                country="NGA", local_government=lc, state=sc)
            store.upsert_party(client.id, entry)
            if status == "B2B":
                created[name] = entry
                n_b2b += 1
            else:
                n_b2c += 1
        msg = f"Saved {n_b2b} B2B and {n_b2c} B2C customer(s)."
        if junk_tin:
            msg += f" {junk_tin} non-TIN value(s) (e.g. 'NOT APPLICABLE') dropped → saved as B2C."
        if incomplete:
            st.warning(
                f"⚠️ {incomplete} B2B customer(s) are missing email / street / state / LGA. "
                "Digitax will reject incomplete parties — complete them and approve again before uploading."
            )
        st.success(msg)
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
            short = [p for p in created_parties
                     if not (p.email_address and p.street_name and p.state and p.local_government)]
            if short:
                st.warning(
                    f"⚠️ {len(short)} new B2B customer(s) are missing email / address / state / LGA "
                    "(Digitax will reject incomplete parties). Complete them in the Customers section "
                    "above and approve again before uploading."
                )
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
    flagged = result.blocking_invoices
    itc = client.invoice_type_code
    total = len(result.invoices)

    # Everything resolved -> one file, no confusing second button.
    if not flagged:
        st.success(f"All {total} invoices are ready to upload to Digitax.")
        st.download_button(
            f"⬇️ {slug}_invoices_{period}.csv  ({total} invoices)",
            data=write_csv_bytes(result.invoices, only_ready=True, invoice_type_code=itc),
            file_name=f"{slug}_invoices_{period}.csv", mime="text/csv", type="primary",
        )
        return

    # Some invoices still have unresolved issues -> two clearly-labelled files.
    st.warning(
        f"**{len(ready)} of {total} invoices are ready.** The other {len(flagged)} still have "
        "unresolved issues (see **Lines needing attention** above) — resolve them, or fix the "
        "second file in Excel."
    )
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Ready only** — safe to upload now.")
        if ready:
            st.download_button(
                f"⬇️ {slug}_invoices_{period}.csv  ({len(ready)} ready)",
                data=write_csv_bytes(result.invoices, only_ready=True, invoice_type_code=itc),
                file_name=f"{slug}_invoices_{period}.csv", mime="text/csv", type="primary",
                use_container_width=True,
            )
        else:
            st.caption("No fully-ready invoices yet.")
    with col2:
        st.markdown(f"**All {total}** — the {len(flagged)} flagged have blanks to fix in Excel.")
        st.download_button(
            f"⬇️ {slug}_invoices_all_{period}.csv  ({total})",
            data=write_csv_bytes(result.invoices, only_ready=False, invoice_type_code=itc),
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
                itc = st.text_input(
                    "invoice_type_code (written to every row)", value=c.invoice_type_code,
                    help="Digitax label for this code: " + (invoice_type_label(c.invoice_type_code) or "unknown"),
                )
                lbl = invoice_type_label(itc.strip())
                if lbl:
                    st.caption(f"`{itc.strip()}` = **{lbl}** per the Digitax invoice-type reference.")
                notes = st.text_area("Notes", value=c.notes)
                if st.form_submit_button("Save client"):
                    store.save_client(ClientConfig(
                        id=c.id, name=name, reader=reader, b2b_expected=b2b,
                        tin_suffix_rule=tin_rule, invoice_type_code=itc.strip() or "388", notes=notes,
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
