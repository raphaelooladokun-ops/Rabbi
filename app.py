"""Rabbi Consult — e-Invoicing Converter (Streamlit operator app).

One run: pick client -> upload raw sales file -> process -> review & resolve
exceptions -> download the Digitax CSV. No code or config editing during
normal use; masters are maintained through the Masters tab and persist
between runs.
"""
from __future__ import annotations

import io
import json
import re
from decimal import Decimal

import pandas as pd
import streamlit as st

from core.audit import KIND_LABELS, new_run_id
from core.backup import export_json, import_all
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
from core import insights, reference
from core.onboarding import classify_vat, find_vat_column, assign_item_codes, STANDARD as VAT_STANDARD
from core.proposals import propose_items, propose_party, run_period, sanitize_tin
from core.reference import invoice_type_label, is_valid_hsn, is_valid_service_code
from core.readers import get_reader
from core.store import get_master_store, using_database
from ui.auth import ALL_CLIENTS, allowed_clients, is_admin, login_gate, logout_button

st.set_page_config(page_title="Rabbi e-Invoicing Converter", page_icon="🧾", layout="wide")

# Bump on each deploy so the sidebar shows whether the latest code is live.
APP_VERSION = "v2026.08.11-reportinsights"

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
        raw_bytes = uploaded.getvalue()
        try:
            read_result = get_reader(client.reader).read(raw_bytes)
        except Exception as exc:  # noqa: BLE001 - surface any reader failure
            st.error(f"Could not read the file: {exc}")
            return
        st.session_state["rows"] = read_result.rows
        st.session_state["file_errors"] = read_result.file_errors
        st.session_state["rows_key"] = cache_key
        st.session_state["raw_bytes"] = raw_bytes
        st.session_state["raw_name"] = uploaded.name

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
    # Render the (potentially large) resolution tables only on demand, so the
    # Convert page is always instant — downloads and the exceptions list are
    # available without waiting for hundreds of editor widgets to build.
    n_items = len(_distinct_flagged(result, FlagCode.ITEM_NOT_FOUND, "item_name"))
    n_custs = len(_distinct_flagged(result, FlagCode.CUSTOMER_NOT_FOUND, "customer_name"))
    n_shared = len(_shared_code_collisions(result)) if is_admin() else 0
    if n_items or n_custs or n_shared:
        st.header("🛠 Resolve flagged items & customers")
        bits = [f"**{n_items} item(s)** and **{n_custs} customer(s)** aren't in the master yet."]
        if n_shared:
            bits.append(f"**{n_shared} item code(s)** are shared by different products (admin fix inside).")
        st.caption(" ".join(bits) + " Open the workspace to resolve them "
                   "(or skip and fix in the downloaded file).")
        if st.toggle("Open the resolve workspace", key=f"show_resolve_{_run_key()}", value=False):
            _render_create_masters(store, client, result)
        st.divider()
    _render_staged_output(store, client, result)


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


def _shared_code_collisions(result: ProcessResult) -> dict[str, list[str]]:
    """Map each item_code that is shared by different item names -> those names."""
    out: dict[str, set] = {}
    for iv in result.invoices:
        for f in iv.all_flags:
            if f.code == FlagCode.SHARED_ITEM_CODE:
                code = f.context.get("shared_code", "")
                if code:
                    out.setdefault(code, set()).update(f.context.get("colliding_names", ()))
    return {code: sorted(names) for code, names in out.items()}


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
    df = pd.DataFrame(report)
    preview = 200
    if len(df) > preview:
        st.caption(f"Showing the first {preview} of {len(df)} — download the full list below.")
        st.dataframe(df.head(preview), use_container_width=True, hide_index=True)
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download this list (CSV)",
        data=write_exceptions_csv(result).encode("utf-8"),
        file_name="exceptions_report.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# Admin: split an item code shared by two different products
# ---------------------------------------------------------------------------
def _render_shared_code_fix(store: MasterStore, client: ClientConfig, result: ProcessResult) -> None:
    collisions = _shared_code_collisions(result)
    if not collisions:
        return

    st.markdown("#### ⚠️ Items sharing a code (admin) — split into separate Digitax items")
    st.caption(
        "These names resolve to the **same** item_code but are different products, so Digitax "
        "rejects the invoice. Tick the name(s) that should become a **brand-new distinct item** "
        "(a fresh ITM_ code, drafted from the shared one). The unticked name keeps the existing "
        "code. Approve, and the run re-resolves automatically."
    )
    picks: list[str] = []
    for ci, (code, names) in enumerate(collisions.items()):
        st.markdown(f"**Code `{code}`** — used by {len(names)} different items:")
        for ni, name in enumerate(names):
            # Default: keep the first name on the existing code, split the rest.
            checked = st.checkbox(
                name, value=(ni != 0), key=f"split_{_run_key()}_{ci}_{ni}",
                help="Tick = mint a new code for this item" if ni != 0
                     else "Unticked = keep this item on the existing code",
            )
            if checked:
                picks.append(name)

    if st.button("✳️ Create selected as new distinct items", key=f"splitbtn_{_run_key()}"):
        if not picks:
            st.warning("Nothing selected — tick at least one name to give it a new code.")
            return
        items_master = store.list_items(client.id)
        # force_new drafts a fresh code for each picked name (from the nearest
        # existing item, which is the shared entry), instead of re-matching it.
        proposals = propose_items(picks, items_master, force_new=picks)
        created_bucket = _bucket("created_items")
        created = 0
        for p in (pp for pp in proposals if pp.kind == "new"):
            entry = ItemEntry(
                name=p.name, item_code=p.item_code,
                hsn_code=reference.normalize_hsn(p.hsn_code),
                tax_category=p.tax_category_code, item_category=p.item_category,
                description=p.description or p.name, is_service=p.is_service)
            store.upsert_item(client.id, entry)
            # Feed the same bucket the "new items" flow uses, so these codes are
            # included in the new-items CSV to upload to Digitax.
            created_bucket[entry.name] = entry
            created += 1
        st.success(f"Created {created} new distinct item(s) with fresh codes — they're included in "
                   "the **new items CSV** below to upload to Digitax. Re-resolving…")
        st.rerun()
    st.divider()


# ---------------------------------------------------------------------------
# Create missing masters
# ---------------------------------------------------------------------------
def _render_create_masters(store: MasterStore, client: ClientConfig, result: ProcessResult) -> None:
    # Admin-only: split item codes wrongly shared by two different products.
    if is_admin():
        _render_shared_code_fix(store, client, result)

    unknown_items = _distinct_flagged(result, FlagCode.ITEM_NOT_FOUND, "item_name")
    unknown_custs = _distinct_flagged(result, FlagCode.CUSTOMER_NOT_FOUND, "customer_name")
    if not unknown_items and not unknown_custs:
        return

    st.caption(
        "New items are matched against the existing master first to avoid duplicate Digitax "
        "items; only genuinely-new ones get a new code. Nothing is auto-trusted — approve below."
    )
    # Tabs keep the two big tasks separate and easy to navigate.
    if unknown_items and unknown_custs:
        t_items, t_custs = st.tabs([f"📦 Items to resolve ({len(unknown_items)})",
                                    f"🧾 Customers to resolve ({len(unknown_custs)})"])
        with t_items:
            _render_item_proposals(store, client, unknown_items)
        with t_custs:
            _render_party_proposals(store, client, result, unknown_custs)
    elif unknown_items:
        st.markdown(f"### 📦 Items to resolve ({len(unknown_items)})")
        _render_item_proposals(store, client, unknown_items)
    else:
        st.markdown(f"### 🧾 Customers to resolve ({len(unknown_custs)})")
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
        st.markdown(f"**New items ({len(new)})** — HSN/category are **drafted from the closest "
                    "existing item** (verify the HSN), then approve.")
        if len(new) > 60:
            st.warning(
                f"{len(new)} items aren't recognised. If **{client.name}** already has an items "
                "list, import it under **Master data** first — that's far faster than approving "
                "hundreds here. Otherwise approve them a page at a time below."
            )
        # Paginate so the editor never renders hundreds of rows at once.
        page_size = 50
        pages = (len(new) + page_size - 1) // page_size
        page = 1
        if pages > 1:
            page = st.number_input(f"Page (1–{pages}, {page_size} per page)", 1, pages, 1,
                                   key=f"newitems_page_{_run_key()}")
        chunk = new[(page - 1) * page_size: page * page_size]
        ndf = pd.DataFrame([
            {"name": p.name, "item_code": p.item_code, "item_category": p.item_category,
             "hsn_code": p.hsn_code, "description": p.description,
             "tax_category_code": p.tax_category_code, "is_service": p.is_service}
            for p in chunk
        ])
        edited = st.data_editor(
            ndf, hide_index=True, use_container_width=True, key=f"newitems_{_run_key()}_{page}",
            disabled=["name", "item_code"],
            column_config={
                "tax_category_code": st.column_config.SelectboxColumn(options=tax_categories),
                "is_service": st.column_config.CheckboxColumn(),
            },
        )
        st.caption("HSN codes are validated against the Digitax HSN reference; unknown ones are flagged but not blocked.")
        if st.button(f"Approve new items on this page ({len(chunk)})", key=f"approveitems_{_run_key()}_{page}"):
            created = _bucket("created_items")
            missing_hsn = bad_hsn = 0
            for _, r in edited.iterrows():
                is_svc = bool(r.get("is_service", False))
                # Products get coerced to the Digitax `xxxx.xx` HSN format;
                # service codes are left as entered (they use a different code set).
                raw_hsn = str(r.get("hsn_code", "")).strip()
                hsn = raw_hsn if is_svc else reference.normalize_hsn(raw_hsn)
                if not hsn:
                    missing_hsn += 1
                elif not (is_valid_service_code(hsn) if is_svc else is_valid_hsn(hsn)):
                    bad_hsn += 1
                entry = ItemEntry(
                    name=str(r["name"]), item_code=str(r["item_code"]),
                    hsn_code=hsn,
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

    # Paginate so the grid never renders hundreds of widget-rows at once.
    page_size = 25
    pages = (len(names) + page_size - 1) // page_size
    page = 1
    if pages > 1:
        page = st.number_input(f"Page (1–{pages}, {page_size} per page)", 1, pages, 1,
                               key=f"pg_{group}_{_run_key()}")
    start = (page - 1) * page_size
    page_rows = list(enumerate(names))[start:start + page_size]  # (global index, name)

    header = st.columns(_GRID_RATIOS)
    for col, h in zip(header, _GRID_HEADERS):
        col.markdown(f"**{h}**")

    for i, name in page_rows:
        tin_hint, addr = hints.get(name, ("", ""))
        prop = propose_party(name, tin_hint=tin_hint, address_text=addr)
        k = f"{group}_{_run_key()}_{i}"  # global index -> stable key across pages
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

    if st.button(f"Approve customers on this page ({len(page_rows)})",
                 key=f"gridbtn_{group}_{_run_key()}_{page}"):
        created = _bucket("created_parties")
        n_b2b = n_b2c = incomplete = junk_tin = 0
        for i, name in page_rows:
            k = f"{group}_{_run_key()}_{i}"
            if not st.session_state.get(f"ap_{k}"):
                continue
            raw_tin = str(st.session_state.get(f"tin_{k}", ""))
            tin = sanitize_tin(raw_tin)  # strips 'TIN:'/spaces; drops malformed
            if raw_tin.strip() and not tin:
                junk_tin += 1
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
            msg += (f" {junk_tin} malformed TIN value(s) (e.g. 'NOT APPLICABLE' or a 'TIN:'-prefixed / "
                    "wrong-length number) dropped → saved as B2C.")
        if incomplete:
            st.warning(
                f"⚠️ {incomplete} B2B customer(s) are missing email / street / state / LGA. "
                "Digitax will reject incomplete parties — complete them and approve again before uploading."
            )
        st.success(msg)
        st.rerun()


# ---------------------------------------------------------------------------
# Staged output (masters first, then invoices) + audit trail
# ---------------------------------------------------------------------------
def _audit_save(store, client, kind: str, filename: str, content: bytes) -> None:
    """Record a downloaded artifact once per run+kind (best-effort).

    Reps always record (for the operator's audit). An admin can opt out per run
    via the toggle on the download step (``audit_on_<run>`` defaults to True).
    """
    if not st.session_state.get(f"audit_on_{_run_key()}", True):
        return
    flag = f"aud_{_run_key()}_{kind}"
    if st.session_state.get(flag):
        return
    run_id = st.session_state.setdefault("run_id_map", {}).setdefault(_run_key(), new_run_id(client.id))
    try:
        store.save_artifact(client.id, run_id, kind, filename, content,
                            st.session_state.get("username", ""))
        st.session_state[flag] = True
    except Exception:  # noqa: BLE001 - never block a download on audit failure
        pass


def _audit_save_upload(store, client) -> None:
    raw = st.session_state.get("raw_bytes")
    if raw:
        _audit_save(store, client, "uploaded_raw", st.session_state.get("raw_name", "upload"), raw)


def _render_staged_output(store, client: ClientConfig, result: ProcessResult) -> None:
    st.subheader("Download")
    # Reps always keep an audit copy; an admin processing on behalf of a client
    # can choose per run whether this run's files are stored in Records.
    if is_admin():
        st.session_state[f"audit_on_{_run_key()}"] = st.toggle(
            "🗄 Store this run's files in Records (audit)", value=True,
            key=f"auditpref_{_run_key()}",
            help="Admin only. Off = the files you download for this run are NOT kept in Records.",
        )
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
            data = write_items_template(created_items).encode("utf-8")
            fname = f"{slug}_new_items_{period}.csv"
            if st.download_button(f"⬇️ {fname}  ({len(created_items)} items)", data=data,
                                  file_name=fname, mime="text/csv", type="primary"):
                _audit_save(store, client, "new_items_csv", fname, data)
        if created_parties:
            short = [p for p in created_parties
                     if not (p.email_address and p.street_name and p.state and p.local_government)]
            if short:
                st.warning(
                    f"⚠️ {len(short)} new B2B customer(s) are missing email / address / state / LGA "
                    "(Digitax will reject incomplete parties). Complete them in the Customers section "
                    "above and approve again before uploading."
                )
            data = write_parties_template(created_parties).encode("utf-8")
            fname = f"{slug}_new_parties_{period}.csv"
            if st.download_button(f"⬇️ {fname}  ({len(created_parties)} customers)", data=data,
                                  file_name=fname, mime="text/csv", type="primary"):
                _audit_save(store, client, "new_parties_csv", fname, data)
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
        data = write_csv_bytes(result.invoices, only_ready=True, invoice_type_code=itc)
        fname = f"{slug}_invoices_{period}.csv"
        if st.download_button(f"⬇️ {fname}  ({total} invoices)", data=data,
                              file_name=fname, mime="text/csv", type="primary"):
            _audit_save(store, client, "invoices_csv", fname, data)
            _audit_save_upload(store, client)
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
            data = write_csv_bytes(result.invoices, only_ready=True, invoice_type_code=itc)
            fname = f"{slug}_invoices_{period}.csv"
            if st.download_button(f"⬇️ {fname}  ({len(ready)} ready)", data=data, file_name=fname,
                                  mime="text/csv", type="primary", use_container_width=True):
                _audit_save(store, client, "invoices_csv", fname, data)
                _audit_save_upload(store, client)
        else:
            st.caption("No fully-ready invoices yet.")
    with col2:
        st.markdown(f"**All {total}** — the {len(flagged)} flagged have blanks to fix in Excel.")
        data = write_csv_bytes(result.invoices, only_ready=False, invoice_type_code=itc)
        fname = f"{slug}_invoices_all_{period}.csv"
        if st.download_button(f"⬇️ {fname}  ({total})", data=data, file_name=fname,
                              mime="text/csv", use_container_width=True):
            _audit_save(store, client, "invoices_csv", fname, data)
            _audit_save_upload(store, client)


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


def _render_master_download(key: str, entries: list, to_row, writer, filename: str, search_of) -> None:
    """Pick rows from a master and download them as a Digitax-ready CSV.

    Tick rows to include; if nothing is ticked, everything currently shown (the
    search-filtered set) is downloaded. ``writer`` is the Digitax template
    writer for that master, so the file is upload-ready.
    """
    if not entries:
        st.caption("Nothing to download yet — the master is empty.")
        return
    q = st.text_input("Filter (name / code / TIN)", key=f"dlq_{key}").strip().lower()
    filtered = [e for e in entries if not q or q in search_of(e).lower()]
    if not filtered:
        st.caption("No rows match that filter.")
        return
    st.caption(f"Showing {len(filtered)} of {len(entries)}. Tick rows to include — "
               "or leave all unticked to download everything shown.")
    disp = pd.DataFrame([{"✓": False, **to_row(e)} for e in filtered])
    edited = st.data_editor(
        disp, hide_index=True, use_container_width=True, key=f"dltbl_{key}",
        disabled=[c for c in disp.columns if c != "✓"],
        column_config={"✓": st.column_config.CheckboxColumn(help="Tick to include in the download")},
    )
    picks = [e for e, (_, r) in zip(filtered, edited.iterrows()) if bool(r["✓"])]
    chosen = picks if picks else filtered
    st.download_button(
        f"⬇️ Download {len(chosen)} as Digitax CSV", data=writer(chosen).encode("utf-8"),
        file_name=filename, mime="text/csv", key=f"dlbtn_{key}",
    )


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

        with st.expander("⬇️ Download selected items as a Digitax CSV"):
            _render_master_download(
                key=f"items_{client.id}",
                entries=items,
                to_row=lambda e: {"name": e.name, "item_code": e.item_code,
                                  "hsn_code": e.hsn_code, "tax_category": e.tax_category},
                writer=write_items_template,
                filename=f"{client.id}_items_digitax.csv",
                search_of=lambda e: f"{e.name} {e.item_code} {e.hsn_code} {e.tax_category}",
            )

        st.divider()
        with st.expander(f"⚠️ Clear items master ({len(items)} items) — start afresh"):
            st.warning(f"Permanently deletes **all {len(items)} items** for {client.name}. "
                       "Download a backup first (Clients & settings → Backup).")
            if st.checkbox("Yes, delete all items for this client", key=f"confirm_clear_items_{client.id}"):
                if st.button("🗑 Clear items master now", key=f"clear_items_{client.id}", type="primary"):
                    store.seed_items(client.id, [])
                    st.success("Items master cleared.")
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

        with st.expander("⬇️ Download selected customers as a Digitax CSV"):
            st.caption("The Digitax party template needs a TIN and full address per row — pick your "
                       "B2B customers here. B2C/cash customers usually aren't uploaded as parties.")
            _render_master_download(
                key=f"parties_{client.id}",
                entries=parties,
                to_row=lambda e: {"name": e.name, "tin": e.tin, "status": e.status,
                                  "state": e.state, "local_government": e.local_government},
                writer=write_parties_template,
                filename=f"{client.id}_parties_digitax.csv",
                search_of=lambda e: f"{e.name} {e.tin} {e.status}",
            )

        st.divider()
        with st.expander(f"⚠️ Clear parties master ({len(parties)} customers) — start afresh"):
            st.warning(f"Permanently deletes **all {len(parties)} customers** for {client.name}. "
                       "Download a backup first (Clients & settings → Backup).")
            if st.checkbox("Yes, delete all customers for this client", key=f"confirm_clear_parties_{client.id}"):
                if st.button("🗑 Clear parties master now", key=f"clear_parties_{client.id}", type="primary"):
                    store.seed_parties(client.id, [])
                    st.success("Parties master cleared.")
                    st.rerun()


# ---------------------------------------------------------------------------
# New client + onboarding items (admin)
# ---------------------------------------------------------------------------
# Reader = the client's sales-file layout. New clients reuse a known layout.
_READER_LABELS = {
    "geeta": "Tally Sales Register (.xlsx)",
    "goldcoin": "Tally Sales Register (.xls, legacy)",
    "friendship": "Flat 'SALES LEDGER' (.xlsx)",
}


def _slug(text: str) -> str:
    """A safe client id: lowercase letters/digits/underscores."""
    s = "".join(ch if ch.isalnum() else "_" for ch in str(text).lower())
    return re.sub(r"_+", "_", s).strip("_")


def _render_new_client(store: MasterStore, key: str = "settings") -> None:
    with st.form(f"new_client_{key}"):
        st.markdown("**Add a new client** — creates its full environment (Convert, Master data, "
                    "Records, insights) automatically.")
        name = st.text_input("Client name")
        cid = st.text_input("Client ID (short; letters/numbers only)",
                            help="Leave blank to auto-generate from the name.")
        reader = st.selectbox("Sales-file format", list(_READER_LABELS),
                              format_func=lambda k: _READER_LABELS[k],
                              help="Which layout the client's raw sales export uses (for Convert).")
        b2b = st.checkbox("Has registered B2B customers", value=True)
        tin_rule = st.text_input("TIN suffix rule (e.g. -0001; blank for none)")
        if st.form_submit_button("Create client"):
            final_id = _slug(cid or name)
            existing = {c.id for c in store.list_clients()}
            if not name.strip():
                st.error("Enter a client name.")
            elif not final_id:
                st.error("Could not derive a valid client ID — enter one explicitly.")
            elif final_id in existing:
                st.error(f"A client with ID '{final_id}' already exists.")
            else:
                store.save_client(ClientConfig(
                    id=final_id, name=name.strip(), reader=reader,
                    b2b_expected=b2b, tin_suffix_rule=tin_rule.strip()))
                st.success(f"Created client '{name.strip()}' ({final_id}). It now has its own "
                           "Convert, Master data and everything else.")
                st.rerun()


def _col_index(cols: list[str], guess: str) -> int:
    return cols.index(guess) if guess in cols else 0


def _opt_index(cols: list[str], guess: str) -> int:
    return (cols.index(guess) + 1) if guess in cols else 0


def _guess_col(cols: list[str], aliases: list[str]) -> str:
    norm = {_norm_col(c): c for c in cols}
    for a in aliases:
        if _norm_col(a) in norm:
            return norm[_norm_col(a)]
    return ""


def _onb_entries(draft: list[dict], codes: list[str]) -> list:
    """Build ItemEntry objects from the staged draft + assigned codes."""
    return [ItemEntry(
        name=d["name"], item_code=codes[i],
        hsn_code=reference.normalize_hsn(d["hsn_code"]),
        tax_category=d["tax_category"] or "STANDARD_VAT",
        item_category=d["item_category"], description=d["description"] or d["name"],
        is_service=bool(d["is_service"]),
    ) for i, d in enumerate(draft)]


def render_onboarding(store: MasterStore) -> None:
    st.header("Onboarding — build a client's items master")
    st.caption("Turn a client's raw product list into a Digitax-ready items master: stage it, set VAT "
               "status (approve all as VATable or mark exceptions), then load it into the client.")

    with st.expander("➕ Create a new client (gives it a full environment)"):
        _render_new_client(store, key="onboarding")

    clients = store.list_clients()
    if not clients:
        st.info("Create a client above to begin onboarding its items.")
        return
    target = st.selectbox("Target client", clients, format_func=lambda c: f"{c.name} ({c.id})",
                          key="onb_target")

    up = st.file_uploader("Raw items list (CSV/XLSX)", type=["csv", "xlsx"], key=f"onb_up_{target.id}")
    table = _import_table(up)
    dkey = f"onb_draft_{target.id}"

    if table is not None and len(table.columns):
        cols = list(table.columns)
        st.markdown("**Map the columns** (we guessed; adjust if needed)")
        c1, c2, c3 = st.columns(3)
        name_col = c1.selectbox("Item name *", cols,
                                index=_col_index(cols, _guess_col(cols, ["item_name", "name", "item name", "product", "description"])),
                                key=f"onb_name_{target.id}")
        desc_col = c2.selectbox("Description (optional)", ["(none)"] + cols,
                                index=_opt_index(cols, _guess_col(cols, ["description", "item description"])),
                                key=f"onb_desc_{target.id}")
        hsn_col = c3.selectbox("HSN code (optional)", ["(none)"] + cols,
                               index=_opt_index(cols, _guess_col(cols, _ITEM_ALIASES["hsn_code"])),
                               key=f"onb_hsn_{target.id}")
        c4, c5 = st.columns(2)
        cat_col = c4.selectbox("Item category (optional)", ["(none)"] + cols,
                               index=_opt_index(cols, _guess_col(cols, ["item_category", "category_name", "category"])),
                               key=f"onb_cat_{target.id}")
        no_vat = "(no VAT column — approve all as VATable)"
        vat_guess = find_vat_column(cols)
        vat_opts = [no_vat] + cols
        vat_col = c5.selectbox("VAT / taxable column", vat_opts,
                               index=(vat_opts.index(vat_guess) if vat_guess in vat_opts else 0),
                               key=f"onb_vat_{target.id}",
                               help="If the list states which items are VATable/exempt, pick that column.")
        has_vat = vat_col in cols

        if st.button("📥 Load & stage items", key=f"onb_stage_{target.id}"):
            rows = []
            for _, r in table.iterrows():
                name = str(r.get(name_col, "")).strip()
                if not name:
                    continue
                raw_v = str(r.get(vat_col, "")).strip() if has_vat else ""
                rows.append({
                    "name": name,
                    "description": (str(r.get(desc_col, "")).strip() if desc_col in cols else "") or name,
                    "hsn_code": reference.normalize_hsn(str(r.get(hsn_col, ""))) if hsn_col in cols else "",
                    "item_category": str(r.get(cat_col, "")).strip() if cat_col in cols else "",
                    "tax_category": classify_vat(raw_v) if has_vat else VAT_STANDARD,
                    "is_service": False,
                    "source_vat": raw_v,
                })
            st.session_state[dkey] = rows
            st.session_state[f"{dkey}_hasvat"] = has_vat
            st.session_state[f"{dkey}_ver"] = st.session_state.get(f"{dkey}_ver", 0) + 1
            st.rerun()

    if st.session_state.get(dkey):
        _render_onboarding_review(store, target, dkey)


def _render_onboarding_review(store: MasterStore, target: ClientConfig, dkey: str) -> None:
    from collections import Counter
    draft = st.session_state[dkey]
    has_vat = st.session_state.get(f"{dkey}_hasvat", False)
    ver = st.session_state.get(f"{dkey}_ver", 0)
    tax_opts = _tax_category_options(store)

    st.divider()
    st.subheader(f"Review {len(draft)} item(s)")
    counts = Counter(d["tax_category"] or "(unset)" for d in draft)
    st.caption(" · ".join(f"**{k}**: {v}" for k, v in counts.items()))
    if has_vat:
        st.info("Your list stated VAT status — it's pre-filled below. Approve or adjust, then load.")
        unclear = sum(1 for d in draft if not d["tax_category"])
        if unclear:
            st.warning(f"{unclear} row(s) had a VAT value we couldn't read — set them before loading.")
    else:
        st.info("Your list didn't state VAT status — everything is **Standard VAT**. Mark exceptions below.")

    # Bulk actions (rendered above the editor; they mutate the staged draft).
    b1, b2, b3 = st.columns([1.4, 2.2, 1.4])
    if b1.button("Mark ALL as Standard VAT", key=f"onb_allstd_{target.id}"):
        for d in draft:
            d["tax_category"] = VAT_STANDARD
        st.session_state[f"{dkey}_ver"] = ver + 1
        st.rerun()
    flt = b2.text_input("Exceptions — filter items (name contains)", key=f"onb_flt_{target.id}")
    setcat = b3.selectbox("Set matches to", tax_opts, key=f"onb_setcat_{target.id}")
    if b2.button("Apply to filtered", key=f"onb_apply_{target.id}"):
        q = flt.strip().lower()
        n = 0
        for d in draft:
            if q and q in d["name"].lower():
                d["tax_category"] = setcat
                n += 1
        st.session_state[f"{dkey}_ver"] = ver + 1
        st.toast(f"Set {n} item(s) to {setcat}.")
        st.rerun()

    mode = st.radio("Load mode", ["Append to existing master", "Replace existing master"],
                    horizontal=True, key=f"onb_mode_{target.id}")
    base = [] if mode.startswith("Replace") else store.list_items(target.id)
    codes = assign_item_codes(base, len(draft))

    df = pd.DataFrame([{
        "item_code": codes[i], "name": d["name"], "hsn_code": d["hsn_code"],
        "tax_category": d["tax_category"], "is_service": d["is_service"],
        "item_category": d["item_category"], "description": d["description"],
    } for i, d in enumerate(draft)])
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"onb_ed_{target.id}_{ver}",
        disabled=["item_code", "name"],
        column_config={
            "tax_category": st.column_config.SelectboxColumn(options=tax_opts),
            "is_service": st.column_config.CheckboxColumn(),
        },
    )
    # Persist manual edits back onto the staged draft (row order preserved).
    for d, r in zip(draft, edited.to_dict("records")):
        d["tax_category"] = str(r["tax_category"] or "")
        d["is_service"] = bool(r["is_service"])
        d["hsn_code"] = reference.normalize_hsn(str(r["hsn_code"])) if str(r["hsn_code"]).strip() else ""
        d["item_category"] = str(r["item_category"]).strip()
        d["description"] = str(r["description"]).strip() or d["name"]
    st.session_state[dkey] = draft

    st.divider()
    lc1, lc2, lc3 = st.columns([2, 2, 1])
    if lc1.button(f"✅ Load {len(draft)} items into {target.name}", type="primary", key=f"onb_load_{target.id}"):
        unclear = [d for d in draft if not d["tax_category"]]
        if unclear:
            st.error(f"{len(unclear)} item(s) still have no VAT category — set them first.")
        else:
            entries = _onb_entries(draft, codes)
            if mode.startswith("Replace"):
                store.seed_items(target.id, entries)
            else:
                for e in entries:
                    store.upsert_item(target.id, e)
            st.success(f"Loaded {len(entries)} items into {target.name}'s master "
                       f"({'replaced' if mode.startswith('Replace') else 'appended'}).")
    lc2.download_button(
        "⬇️ Download as Digitax items CSV",
        data=write_items_template(_onb_entries(draft, codes)).encode("utf-8"),
        file_name=f"{target.id}_items_digitax.csv", mime="text/csv", key=f"onb_dl_{target.id}")
    if lc3.button("Clear staged list", key=f"onb_clear_{target.id}"):
        st.session_state.pop(dkey, None)
        st.rerun()


# ---------------------------------------------------------------------------
# Records / audit trail (admin)
# ---------------------------------------------------------------------------
def render_records(store: MasterStore) -> None:
    st.header("Records — audit trail")
    st.caption("Every file a client uploads and every CSV the app generates is kept here when it is "
               "downloaded, for your records.")
    clients = store.list_clients()
    options = ["(all clients)"] + [c.id for c in clients]
    fcol = st.columns([2, 1, 1])
    sel = fcol[0].selectbox("Client", options)
    client_id = None if sel == options[0] else sel
    # Optional date window (leave a box empty to leave that side open). Records
    # are stamped in UTC; matching is on the calendar date of created_at.
    date_from = fcol[1].date_input("From", value=None, key="records_from",
                                    help="Show records on/after this date. Leave blank for no start.")
    date_to = fcol[2].date_input("To", value=None, key="records_to",
                                 help="Show records on/before this date. Leave blank for no end.")

    try:
        artifacts = store.list_artifacts(client_id)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read records: {exc}")
        return
    if not artifacts:
        st.info("No files recorded yet. They appear here after a client downloads an invoices/CSV file.")
        return

    if date_from or date_to:
        lo = date_from.isoformat() if date_from else ""
        hi = date_to.isoformat() if date_to else ""
        # created_at is ISO ("2026-07-15T..."), so its first 10 chars are the date.
        artifacts = [a for a in artifacts
                     if (not lo or a.created_at[:10] >= lo)
                     and (not hi or a.created_at[:10] <= hi)]
        if not artifacts:
            st.info("No records fall within that date range. Widen or clear the From/To dates.")
            return

    # Paginate so the page stays fast as records grow; render a download button
    # on each row (blobs are fetched only for the rows shown on this page).
    page_size = 25
    pages = (len(artifacts) + page_size - 1) // page_size
    page = 1
    top = st.columns([3, 1])
    top[0].caption(f"{len(artifacts)} record(s)")
    if pages > 1:
        page = top[1].number_input(f"Page (1–{pages})", 1, pages, 1, key="records_page")
    rows = artifacts[(page - 1) * page_size: page * page_size]

    ratios = [2.2, 1.4, 1.8, 3.4, 1.4, 0.9, 1.2]
    head = st.columns(ratios)
    for col, label in zip(head, ["When (UTC)", "Client", "Type", "File", "By", "KB", ""]):
        col.markdown(f"**{label}**")
    for a in rows:
        c = st.columns(ratios)
        c[0].write(a.created_at)
        c[1].write(a.client_id)
        c[2].write(a.label)
        c[3].write(a.filename)
        c[4].write(a.username or "—")
        c[5].write(round(a.size / 1024, 1))
        try:
            _fname, content = store.read_artifact(a.id)
            c[6].download_button("⬇️", data=content, file_name=_fname,
                                 key=f"dl_{a.id}", help=f"Download {_fname}")
        except Exception:  # noqa: BLE001
            c[6].caption("n/a")


# ---------------------------------------------------------------------------
# Customer insights (admin)
# ---------------------------------------------------------------------------
def _render_report_upload(store: MasterStore, clients: list) -> None:
    """Admin: add a Digitax invoice report to a client's insights.

    For clients who create invoices on Digitax directly (not via our converter),
    their downloaded invoice reports feed the same insights. Each report is kept
    in Records; invoices are de-duplicated by number, so overlapping months are
    safe to upload.
    """
    with st.expander("➕ Add a Digitax invoice report (for clients who invoice on Digitax directly)"):
        if not clients:
            st.info("Create a client first.")
            return
        target = st.selectbox("Assign to client", clients,
                              format_func=lambda c: f"{c.name} ({c.id})", key="rep_client")
        up = st.file_uploader("Digitax invoice report (CSV)", type=["csv"], key="rep_up")
        if up is None:
            return
        content = up.getvalue()
        summary = insights.summarize_report(content)
        if not summary["ok"]:
            st.error("That doesn't look like a Digitax invoice report — it needs 'Customer Name' "
                     "and 'Invoice Number' columns.")
            return
        st.success(f"Recognised **{summary['invoices']}** invoices across **{summary['customers']}** "
                   f"named customers.")
        if st.button(f"Add to {target.name}'s insights", key="rep_add", type="primary"):
            try:
                store.save_artifact(target.id, new_run_id(target.id), "digitax_report",
                                    up.name, content, st.session_state.get("username", ""))
                st.session_state.pop("_ins_cache", None)  # force recompute
                st.success(f"Added to {target.name}'s insights and saved in Records.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not save the report: {exc}")


def render_insights(store: MasterStore) -> None:
    st.header("Customer insights")
    st.caption("Who each client does the most business with, built from the raw sales files kept in "
               "Records **and** any Digitax invoice reports you add below. Admin-only; it never "
               "touches the client's workflow.")

    clients = store.list_clients()
    _render_report_upload(store, clients)

    options = ["(all clients)"] + [c.id for c in clients]
    top = st.columns([2, 1.4, 1.4, 1])
    sel = top[0].selectbox("Client", options, key="ins_client")
    client_id = None if sel == options[0] else sel
    rank_by = top[1].radio("Rank by", ["Invoices", "Total value"], horizontal=True, key="ins_rank")
    include = top[2].radio("Include", ["B2B only", "All customers"], horizontal=True, key="ins_incl")
    if top[3].button("↻ Refresh", key="ins_refresh", help="Recompute from the latest records"):
        st.session_state.pop("_ins_cache", None)

    # Re-parsing the stored uploads is the expensive bit; cache it and only
    # recompute when the set of records (or the client filter) changes.
    sig = (client_id, insights.records_signature(store, client_id))
    cache = st.session_state.get("_ins_cache")
    if not cache or cache.get("sig") != sig:
        with st.spinner("Reading stored records…"):
            stats = insights.customer_stats(store, client_id)
        st.session_state["_ins_cache"] = {"sig": sig, "stats": stats}
    else:
        stats = cache["stats"]

    if not stats:
        st.info("No data yet for this client. Insights appear once runs are kept in Records "
                "(the 'Store this run's files' toggle on Convert), or add a Digitax invoice "
                "report above.")
        return

    if include == "B2B only":
        stats = [s for s in stats if s.kind == "B2B"]
    if rank_by == "Total value":
        stats = sorted(stats, key=lambda s: (s.total_ex_vat, s.invoices), reverse=True)
    else:
        stats = sorted(stats, key=lambda s: (s.invoices, s.total_ex_vat), reverse=True)
    if not stats:
        st.info("No customers match this filter. Switch 'Include' to All customers.")
        return

    total_val = sum((s.total_ex_vat for s in stats), Decimal("0"))
    m = st.columns(3)
    m[0].metric("Customers", len(stats))
    m[1].metric("Invoices", sum(s.invoices for s in stats))
    m[2].metric("Total value (pre-VAT)", f"₦{total_val:,.2f}")

    rows = [{
        "Rank": i + 1,
        "Client": s.client_id,
        "Customer": s.customer_name,
        "Type": s.kind or "—",
        "Invoices": s.invoices,
        "Total value (pre-VAT)": float(s.total_ex_vat),
        "Biggest invoice #": s.top_invoice_number,
        "Biggest invoice value": float(s.top_invoice_value),
        "First seen": s.first_date.isoformat() if s.first_date else "",
        "Last seen": s.last_date.isoformat() if s.last_date else "",
    } for i, s in enumerate(stats)]
    df = pd.DataFrame(rows)
    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "Total value (pre-VAT)": st.column_config.NumberColumn(format="₦%.2f"),
            "Biggest invoice value": st.column_config.NumberColumn(format="₦%.2f"),
        },
    )
    st.download_button(
        "⬇️ Download insights (CSV)",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"customer_insights_{client_id or 'all'}.csv",
        mime="text/csv", key="ins_dl",
    )


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
def render_settings(store: MasterStore) -> None:
    st.header("Clients & settings")

    st.subheader("Backup & restore")
    st.caption("A portable safety copy of every client's items, customers, tax rates and settings — "
               "independent of where the data is stored.")
    from datetime import date as _date
    bcol1, bcol2 = st.columns(2)
    with bcol1:
        st.download_button(
            "⬇️ Download full masters backup (JSON)",
            data=export_json(store).encode("utf-8"),
            file_name=f"rabbi_masters_backup_{_date.today().isoformat()}.json",
            mime="application/json", use_container_width=True,
        )
    with bcol2:
        with st.expander("Restore from a backup file"):
            st.warning("Restoring overwrites current masters with the backup's contents.")
            up = st.file_uploader("Backup JSON", type=["json"], key="restore_backup")
            if up is not None and st.button("Restore now"):
                try:
                    summary = import_all(store, json.loads(up.getvalue()))
                    st.success(f"Restored {summary['clients']} clients, {summary['items']} items, "
                               f"{summary['parties']} customers.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not restore: {exc}")

    st.divider()
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
    with st.expander("➕ Add a new client"):
        _render_new_client(store)
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


# ---------------------------------------------------------------------------
# How-to page
# ---------------------------------------------------------------------------
def render_howto(is_admin_user: bool) -> None:
    st.header("How to use this app")
    st.markdown(
        """
**Each run takes a few minutes:**

1. **Pick your client** in the sidebar (you only see the clients assigned to you).
2. Go to **Convert** and **upload the client's raw sales file** (`.xlsx`/`.xls`).
3. The app processes it and shows two things:
   - invoices that are **ready**, and
   - a list of **lines needing attention**.
4. **Create missing masters** (only if something is flagged):
   - **Items** — confirm the *possible duplicates* (they map to an item Digitax
     already knows), and for genuinely new items check the drafted **HSN /
     category** and approve. New items get the next code automatically.
   - **Customers** — for any **B2B** customer fill **TIN + email + address**,
     then pick the **State** (the **LGA** list narrows to that state) and
     approve. Customers with no TIN stay **B2C** — that's fine.
5. **Download & upload in order:**
   - If new items/customers were created, download those CSVs first and
     **upload them to Digitax**, then tick **"Done — I've uploaded these."**
   - Then download the **invoices CSV** and upload it to Digitax.
6. You're done. Anything you added is **remembered** for next time.

**Tips**
- A blank/"NOT APPLICABLE" TIN is treated as B2C — never invented.
- The *source_rows* column in the exceptions list points to the exact row in
  your original sheet, if you'd rather fix something there.
- Your additions are saved to the shared master, so the next person sees them.
"""
    )
    if is_admin_user:
        st.info(
            "**Admin only:** seed each client's item & party masters once under **Master data** "
            "(import your existing sheets). After that they grow automatically as items/customers "
            "are resolved. Manage logins, tax rates and invoice_type_code under **Clients & settings**."
        )


# ---------------------------------------------------------------------------
def main() -> None:
    if not login_gate():
        return
    logout_button()
    store = get_store()

    admin = is_admin()
    allowed = allowed_clients()
    clients = store.list_clients()
    if allowed != ALL_CLIENTS:
        allowed_set = set(allowed or [])
        clients = [c for c in clients if c.id in allowed_set]

    pages = (["Convert", "How-to"] if not admin
             else ["Convert", "Master data", "Onboarding", "Records", "Customer insights",
                   "Clients & settings", "How-to"])
    with st.sidebar:
        st.header("Rabbi Consult")
        if using_database():
            st.success("💾 Storage: Database (saved permanently)")
        else:
            st.warning("⚠️ Storage: Local (resets on restart — connect DATABASE_URL)")
        st.caption(f"Build {APP_VERSION}")
        page = st.radio("Page", pages)
        client = None
        if page in ("Convert", "Master data"):
            if not clients:
                st.warning("No clients are assigned to your account. Ask an admin.")
            else:
                options = {c.name: c for c in clients}
                choice = st.selectbox("Client", list(options.keys()))
                client = options[choice]

    if page == "Convert":
        if client:
            render_convert(store, client)
    elif page == "Master data":
        if client:
            render_masters(store, client)
    elif page == "Onboarding":
        render_onboarding(store)
    elif page == "Records":
        render_records(store)
    elif page == "Customer insights":
        render_insights(store)
    elif page == "Clients & settings":
        render_settings(store)
    else:
        render_howto(admin)


if __name__ == "__main__":
    main()
