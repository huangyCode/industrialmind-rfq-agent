"""Review workbench: triage, evidence and the numbers behind the price; edit, recalculate, approve."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from rfq_agent import app_api
from rfq_agent.models import DrawingSpec, ReviewDecision, ReviewEdit, ValidationIssue
from rfq_agent.ui import common
from rfq_agent.ui.logic import (
    cost_detail_frame,
    price_changes,
    price_matrix,
    routing_edits,
    routing_frame,
    trace_frame,
    trace_totals,
)

PLANTS = ["DE", "PL", "CN"]
TABS = ["Extraction", "Issues", "Similar parts", "BOM", "Routing", "Cost"]
AUTO_PLANT = "auto (recommended)"


# ---------- header ----------


def _header(view: app_api.RunView) -> None:
    req = view.request
    c1, c2 = st.columns([3, 2])
    with c1:
        st.title(view.rfq_id)
        if req:
            st.markdown(
                f"**{req.customer_name}**"
                + (f" · {req.contact_name}" if req.contact_name else "")
                + f" · language `{req.language}`"
                + (f" · Incoterm {req.incoterm}" if req.incoterm else "")
                + (f" · received {req.received_at}" if req.received_at else "")
            )
        if req and req.special_requirements:
            st.caption("Customer requirements: " + "; ".join(req.special_requirements))
    with c2:
        b = st.columns(3)
        with b[0]:
            common.status_badge(view.status)
        with b[1]:
            if view.draft:
                st.badge(f"draft v{view.draft.version}", color="gray")
        with b[2]:
            st.badge(f"extractor: {view.extractor}", color="gray")
        if view.pending_review:
            st.caption("Waiting for engineer review.")
        if view.reviews:
            last = view.reviews[-1]
            st.caption(
                f"Last decision: {last.get('action')} by {last.get('reviewer')} ({last.get('at', '')})"
            )
    if view.error:
        st.error(view.error)
    if view.review_error:
        st.error(f"Last edit was rejected: {view.review_error}")


def _confidence(ln) -> None:
    conf = ln.confidence
    tier = conf.tier.value
    with st.container(border=True):
        c0, c1 = st.columns([1.2, 4])
        with c0:
            common.tier_badge(tier)
            st.metric("Overall confidence", f"{conf.overall:.2f}")
        with c1:
            comps = [
                ("Extraction", conf.extraction),
                ("Similarity", conf.similarity),
                ("Coverage", conf.coverage),
                ("Price sanity", conf.price_sanity),
            ]
            if conf.bom_match is not None:
                comps.append(("BOM match", conf.bom_match))
            cols = st.columns(len(comps))
            for col, (name, v) in zip(cols, comps, strict=True):
                col.metric(name, f"{v:.2f}")
                col.progress(min(max(v, 0.0), 1.0))
            if conf.reasons:
                st.markdown("**Why this tier**")
                st.markdown("\n".join(f"- {r}" for r in conf.reasons))
            else:
                st.caption("No findings lowered the confidence.")


# ---------- tabs ----------


def _drawing(render_paths: list[str], item) -> None:
    paths = [Path(p) for p in render_paths if Path(p).exists()]
    if item is not None:
        st.markdown(
            f"**Line {item.line_no}** · {item.customer_part_number or item.drawing_ref} · "
            f"qty {', '.join(f'{q:,}' for q in item.quantities) or '-'}"
            + (f" · due {item.requested_delivery}" if item.requested_delivery else "")
        )
    if not paths:
        st.info("No drawing preview available.")
        return
    st.image(str(paths[0]), caption=paths[0].name, width="stretch")
    if len(paths) > 1:
        with st.expander(f"Regions sent to the model ({len(paths) - 1})"):
            for p in paths[1:]:
                st.image(str(p), caption=p.name, width="stretch")


def _extraction(spec: DrawingSpec | None, uncovered: list[str]) -> None:
    if spec is None:
        st.warning("No drawing data for this line.")
        return
    for w in spec.extraction_warnings:
        st.warning(f"Extraction warning: {w}")
    for f in uncovered:
        st.warning(f"Feature not covered by any routing rule: {f}")
    tb = spec.title_block
    st.markdown("**Title block** (value → text on the drawing it was read from)")
    rows = []
    for name in (
        "part_number",
        "revision",
        "title",
        "material",
        "general_tolerance",
        "default_ra_um",
        "scale",
    ):
        ev = tb.evidence.get(name)
        rows.append(
            {
                "field": name,
                "value": getattr(tb, name),
                "evidence": ev.text if ev else None,
                "location": ev.location if ev else None,
            }
        )
    rows.append({"field": "material_code (master data)", "value": tb.material_code, "evidence": None})
    st.dataframe(pd.DataFrame(rows).astype(str).replace("None", ""), hide_index=True, width="stretch")
    env = spec.envelope
    st.caption(
        f"Shape: {env.shape_class.value} · "
        + " · ".join(
            f"{k.replace('_mm', '')} {v:g} mm"
            for k, v in env.model_dump().items()
            if k != "shape_class" and v is not None
        )
        + (f" · heat treatment: {spec.heat_treatment}" if spec.heat_treatment else "")
        + (f" · surface: {spec.surface_treatment}" if spec.surface_treatment else "")
    )
    if spec.features:
        st.markdown(f"**Features** ({len(spec.features)})")
        feats = []
        for f in spec.features:
            tol = f.tolerance
            tol_s = ""
            if tol:
                tol_s = " ".join(
                    x
                    for x in (
                        tol.fit or "",
                        f"{tol.upper:+g}/{tol.lower:+g}"
                        if tol.upper is not None and tol.lower is not None
                        else "",
                        f"IT{tol.it_grade}" if tol.it_grade else "",
                    )
                    if x
                )
            feats.append(
                {
                    "id": f.id,
                    "type": f.type.value,
                    "model wording": f.description,
                    "nominal mm": f.nominal_mm,
                    "length mm": f.length_mm,
                    "qty": f.quantity,
                    "tolerance": tol_s,
                    "Ra µm": f.ra_um,
                    "thread": f.thread_spec,
                    "evidence": f.evidence.text if f.evidence else None,
                    "where": f.evidence.location if f.evidence else None,
                }
            )
        st.dataframe(pd.DataFrame(feats), hide_index=True, width="stretch")
    if spec.parts_list:
        st.markdown("**Parts list**")
        st.dataframe(
            pd.DataFrame([p.model_dump() for p in spec.parts_list]), hide_index=True, width="stretch"
        )
    if spec.notes:
        st.markdown("**Drawing notes**")
        st.markdown("\n".join(f"- {n}" for n in spec.notes))


def _issues(issues: list[ValidationIssue]) -> None:
    if not issues:
        st.success("No validation or DFM findings.")
        return
    order = {"blocker": 0, "warning": 1, "info": 2}
    svc = common.service()
    for i, iss in enumerate(sorted(issues, key=lambda x: order.get(x.severity.value, 3))):
        with st.container(border=True):
            c1, c2 = st.columns([1, 6])
            with c1:
                st.badge(iss.severity.value.upper(), color=common.SEV_COLOR.get(iss.severity.value, "gray"))
                st.caption(iss.code)
            with c2:
                st.markdown(f"**{iss.message}**")
                st.markdown(f"Suggestion: {iss.suggestion}")
                if iss.field_path:
                    st.caption(f"field: `{iss.field_path}`")
                if iss.kb_ref:
                    with st.expander(f"Knowledge base: {iss.kb_ref}", key=f"kb_{i}_{iss.code}"):
                        chunk = svc.kb_get(iss.kb_ref)
                        if chunk:
                            st.caption(f"{chunk['file']} › {chunk['heading']}")
                            st.markdown(chunk["text"])
                        else:
                            st.caption("Referenced section not found in the knowledge base.")


def _similar(ln) -> None:
    reuse = [i for i in ln.issues if i.code.startswith("REUSE")]
    for r in reuse:
        st.success(f"**Reuse hint ({r.code})**: {r.message}  \n{r.suggestion}")
    sims = ln.similar_parts
    if not sims:
        st.info("No historical part above the minimum similarity score; routing is rule-based only.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "part": s.part_number,
                    "title": s.title,
                    "family": s.family,
                    "material": s.material_code,
                    "score": round(s.score, 3),
                    "ref. unit cost EUR": s.ref_unit_cost_eur,
                    "ref. qty": s.ref_qty,
                    "ref. plant": s.ref_plant,
                }
                for s in sims
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.markdown("**Score breakdown** (per criterion, 0-1)")
    bd = pd.DataFrame({s.part_number: s.score_breakdown for s in sims})
    bd.index.name = "criterion"
    st.bar_chart(bd, horizontal=True, stack=False, height=320)
    if ln.price_deviation_pct is not None:
        st.caption(
            f"Price of this quote vs. the scaled cost of the closest part: {ln.price_deviation_pct:+.0%} "
            "(flagged above ±30%)."
        )


def _bom(ln) -> None:
    if not ln.bom:
        st.info("No BOM for this line.")
        return
    df = pd.DataFrame(
        [
            {
                "item": b.item_no,
                "part number": b.part_number,
                "description": b.description,
                "qty per": b.qty_per,
                "unit": b.unit,
                "make/buy": b.make_or_buy.value,
                "source": b.source,
                "matched to": b.matched_ref,
                "match method": b.match_method,
                "unit cost EUR": b.unit_cost_eur,
                "confidence": round(b.confidence, 2),
                "note": b.note,
            }
            for b in ln.bom
        ]
    )
    unpriced = (df["source"] == "new") | (df["unit cost EUR"].isna() & (df["source"] != "service"))

    def row_style(row):
        return ["background-color: #f9d5d3; color: #1d2733" if unpriced[row.name] else "" for _ in row]

    st.dataframe(df.style.apply(row_style, axis=1).format(precision=3), hide_index=True, width="stretch")
    if unpriced.any():
        st.error(
            f"{int(unpriced.sum())} item(s) could not be matched or priced (highlighted). "
            "They are quoted as 'price on request' and listed in the assumptions."
        )
    st.caption(
        "Service rows (heat treatment, coating) are informational here; they are priced from the routing."
    )


def _routing(ln, editor_key: str, editable: bool) -> list[ReviewEdit]:
    st.caption(
        "Times in minutes. basis: rule = routing rule only · blend = rule blended with the closest "
        "historical part (weight w) · manual = set by an engineer. Suggested operations come from the "
        "reference part and are not costed until accepted. Edit setup/cycle, tick 'accept', delete a row "
        "to remove an operation or add a row (op code + work center) to add one."
    )
    df = routing_frame(ln.routing)
    edited = st.data_editor(
        df,
        key=editor_key,
        hide_index=True,
        width="stretch",
        num_rows="dynamic" if editable else "fixed",
        height=min(38 + 35 * (len(df) + (1 if editable else 0)), 700),
        disabled=not editable or ["basis", "rule_cycle_min", "blend_weight", "rules", "status"],
        column_config={
            "seq": st.column_config.NumberColumn("seq", step=1, format="%d"),
            "setup_min": st.column_config.NumberColumn("setup min", min_value=0.0, step=0.1, format="%.2f"),
            "cycle_min": st.column_config.NumberColumn("cycle min", min_value=0.0, step=0.01, format="%.2f"),
            "rule_cycle_min": st.column_config.NumberColumn("rule cycle", format="%.2f"),
            "blend_weight": st.column_config.NumberColumn("w", format="%.2f"),
            "accept": st.column_config.CheckboxColumn("accept", help="Accept a suggested operation"),
        },
    )
    edits = routing_edits(ln.line_no, ln.routing, edited) if editable else []
    if edits:
        st.info(f"{len(edits)} unapplied routing change(s). Use 'Apply edits & recalculate' below.")
    return edits


def _cost(ln, raw: dict) -> None:
    rec = ln.recommended_plant
    st.markdown(
        f"Recommended plant: **{rec or 'none feasible'}**"
        + (" (reviewer override)" if raw.get("plant_overridden") else "")
        + " · unit price in EUR per piece"
    )
    st.dataframe(price_matrix(ln.costs, rec, PLANTS), hide_index=True, width="stretch")
    reasons = {c.plant: c.infeasible_reason for c in ln.costs if not c.feasible and c.infeasible_reason}
    for p, why in reasons.items():
        st.error(f"Plant {p} excluded: {why}")
    late = sorted({f"{c.plant}@{c.qty}" for c in ln.costs if c.feasible and c.meets_due_date is False})
    if late:
        st.warning("Cannot meet the requested delivery date: " + ", ".join(late))
    st.markdown("**Cost build-up** (every line: formula and data source)")
    feasible = [c for c in ln.costs if c.feasible]
    feasible.sort(key=lambda c: (c.plant != rec, c.plant, c.qty))
    for n, c in enumerate(feasible):
        title = (
            f"{c.plant} @ {c.qty:,} pcs: unit cost {c.unit_cost_eur:,.2f} → unit price {c.unit_price_eur:,.2f} EUR"
            f" · total {c.total_price_eur:,.2f} EUR · lead time {c.lead_time_weeks} wk"
        )
        with st.expander(title, expanded=n == 0):
            detail = cost_detail_frame(c)
            st.dataframe(
                detail,
                hide_index=True,
                width="stretch",
                height=38 + 35 * len(detail),
                column_config={"description": st.column_config.TextColumn(width="medium")},
            )
            total = sum(x.amount_eur_per_pc for x in c.lines)
            st.caption(
                f"Σ lines = {total:,.2f} EUR/pc"
                + (" · minimum order value applied" if c.min_order_applied else "")
                + (f" · meets due date: {c.meets_due_date}" if c.meets_due_date is not None else "")
            )


# ---------- actions ----------


def _decide(rid: str, decision: ReviewDecision, before_prices: dict | None = None) -> None:
    svc = common.service()
    try:
        with (
            common.run_lock(),
            st.spinner("Recalculating ..." if decision.action == "edit" else "Working ..."),
        ):
            view = svc.review(rid, decision)
    except (app_api.EditError, ValueError) as e:
        st.session_state["review_msg"] = ("error", str(e))
        return
    except Exception as e:  # noqa: BLE001
        st.session_state["review_msg"] = ("error", f"{type(e).__name__}: {e}")
        return
    msg = {
        "edit": f"Edits applied, draft recalculated (v{view.draft.version if view.draft else '?'}).",
        "approve": f"Approved. Quote {view.quote.quote_id if view.quote else ''} created.",
        "reject": "RFQ rejected.",
        "request_clarification": "Clarification e-mail drafted.",
    }[decision.action]
    if view.review_error:
        st.session_state["review_msg"] = ("error", view.review_error)
    else:
        st.session_state["review_msg"] = ("success", msg)


def _actions(view: app_api.RunView, ln, raw: dict, routing_changes: list[ReviewEdit]) -> None:
    rid = view.rfq_id
    draft = view.draft
    st.subheader("Engineer decision")
    with st.container(border=True):
        c1, c2, c3 = st.columns([1.3, 1, 1.2])
        reviewer = c1.text_input(
            "Reviewer", value=st.session_state.get("reviewer", "M. Weber"), key="reviewer"
        )
        margin = c2.number_input(
            "Margin %",
            min_value=0.0,
            max_value=89.0,
            step=0.5,
            value=round(draft.margin_pct * 100, 2),
            key=f"margin_{rid}_{view.review_round}",
        )
        feasible = sorted({c.plant for c in ln.costs if c.feasible})
        options = [AUTO_PLANT, *feasible]
        current = ln.recommended_plant if raw.get("plant_overridden") else AUTO_PLANT
        plant = c3.selectbox(
            f"Plant for line {ln.line_no}",
            options,
            index=options.index(current) if current in options else 0,
            key=f"plant_{rid}_{ln.line_no}_{view.review_round}",
        )
        comment = st.text_input(
            "Comment (sent to the customer with 'Request clarification', stored with every decision)",
            key=f"comment_{rid}",
        )
        edits = list(routing_changes)
        if abs(margin / 100 - draft.margin_pct) > 1e-9:
            edits.append(
                ReviewEdit(line_no=ln.line_no, target="margin", field="margin_pct", new_value=margin)
            )
        if plant != current:
            edits.append(
                ReviewEdit(
                    line_no=ln.line_no,
                    target="plant",
                    field="plant",
                    new_value="auto" if plant == AUTO_PLANT else plant,
                )
            )
        b = st.columns(4)
        who = reviewer.strip() or "engineer"
        if b[0].button(
            f"Apply edits & recalculate ({len(edits)})",
            type="primary",
            disabled=not edits,
            width="stretch",
            key="btn_edit",
        ):
            _decide(rid, ReviewDecision(action="edit", reviewer=who, edits=edits, comment=comment or None))
            st.rerun()
        if b[1].button("Approve", width="stretch", key="btn_approve"):
            if edits:
                st.session_state["review_msg"] = (
                    "warning",
                    "There are unapplied edits. Apply them (or undo them) before approving.",
                )
            else:
                _decide(rid, ReviewDecision(action="approve", reviewer=who, comment=comment or None))
            st.rerun()
        if b[2].button("Reject", width="stretch", key="btn_reject"):
            _decide(rid, ReviewDecision(action="reject", reviewer=who, comment=comment or None))
            st.rerun()
        if b[3].button("Request clarification", width="stretch", key="btn_clarify"):
            _decide(
                rid, ReviewDecision(action="request_clarification", reviewer=who, comment=comment or None)
            )
            st.rerun()


def _message() -> None:
    msg = st.session_state.pop("review_msg", None)
    if msg:
        kind, text = msg
        getattr(st, kind)(text)


def _last_change(rid: str) -> None:
    fb = common.service().feedback(rid)
    view = common.service().get(rid)
    if view and view.trace:  # only feedback of the current run (a re-run starts a new draft)
        start = view.trace[0].started_at
        fb = [r for r in fb if r.created_at.replace(tzinfo=None) >= start.replace(tzinfo=None)]
    if not fb:
        return
    last_round = max(r.after.get("review_round", 0) for r in fb)
    recs = [r for r in fb if r.after.get("review_round", 0) == last_round] or fb[-1:]
    with st.container(border=True):
        st.markdown(f"**What changed in the last edit** (by {recs[-1].reviewer}, recorded as feedback)")
        for r in recs:
            other = [d for d in r.diff if not d["path"].startswith("unit_price[")]
            if other:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "line": r.line_no,
                                "field": d["path"],
                                "before": d["before"],
                                "after": d["after"],
                            }
                            for d in other
                        ]
                    ).astype(str),
                    hide_index=True,
                    width="stretch",
                )
            prices = price_changes(r.diff)
            if not prices.empty:
                st.dataframe(prices, hide_index=True, width="stretch")


# ---------- page ----------


def render() -> None:
    _message()
    try:
        svc = common.service()
        runs = svc.list_runs()
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not load runs: {type(e).__name__}: {e}")
        return
    if not runs:
        st.title("Review workbench")
        st.info("No runs yet. Start one in the Inbox.")
        return
    ids = [r["rfq_id"] for r in runs]
    default = next((r["rfq_id"] for r in runs if r["pending_review"]), ids[0])
    pick_col, _ = st.columns([1, 3])
    with pick_col:
        rid = common.rfq_picker(ids, "review_pick", default, label_visibility="collapsed")
    view = svc.get(rid)
    if view is None:
        st.warning(f"No run for {rid}.")
        return
    _header(view)

    raw_by_no = {ln["line_no"]: ln for ln in view.lines}
    if view.draft is None:
        _no_draft(view, raw_by_no)
        _history(view)
        return

    lines = view.draft.lines
    if len(lines) > 1:
        no = st.radio(
            "Line",
            [ln.line_no for ln in lines],
            format_func=lambda n: f"Line {n}",
            horizontal=True,
            key=f"line_{rid}",
        )
    else:
        no = lines[0].line_no
    ln = next(x for x in lines if x.line_no == no)
    raw = raw_by_no.get(no, {})
    _confidence(ln)

    left, right = st.columns([2, 5])
    with left:
        _drawing(ln.render_paths, ln.item)
    with right:
        default_tab = st.query_params.get("tab")
        tabs = st.tabs(TABS, default=default_tab if default_tab in TABS else None)
        with tabs[0]:
            _extraction(ln.spec, ln.uncovered_features)
        with tabs[1]:
            _issues(ln.issues)
        with tabs[2]:
            _similar(ln)
        with tabs[3]:
            _bom(ln)
        with tabs[4]:
            changes = _routing(ln, f"routing_{rid}_{no}_{view.review_round}", view.pending_review)
        with tabs[5]:
            _cost(ln, raw)

    if view.review_round:
        _last_change(rid)
    if view.pending_review:
        _actions(view, ln, raw, changes)
    elif view.quote:
        st.success(f"Approved by {view.quote.approved_by}. Quote {view.quote.quote_id}.")
        if st.button("Open quote", key="open_quote"):
            common.goto("quote", rid)
    _history(view)


def _no_draft(view: app_api.RunView, raw_by_no: dict) -> None:
    st.info(
        "No price was calculated for this RFQ: a blocker was found before costing."
        if view.clarification
        else "No draft available for this run."
    )
    if view.clarification and st.button("Open clarification e-mail", key="open_clar"):
        common.goto("quote", view.rfq_id)
    for no, raw in sorted(raw_by_no.items()):
        spec = DrawingSpec.model_validate(raw["spec"]) if raw.get("spec") else None
        left, right = st.columns([2, 3])
        item = view.request.items[no - 1] if view.request and len(view.request.items) >= no else None
        with left:
            _drawing(raw.get("render_paths", []), item)
        with right:
            tabs = st.tabs(["Issues", "Extraction"])
            with tabs[0]:
                _issues([i for i in view.issues if i.line_no == no])
            with tabs[1]:
                _extraction(spec, [])


def _history(view: app_api.RunView) -> None:
    if view.reviews:
        with st.expander(f"Decision history ({len(view.reviews)})"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "at": r.get("at"),
                            "action": r.get("action"),
                            "reviewer": r.get("reviewer"),
                            "edits": len(r.get("edits") or []),
                            "comment": r.get("comment"),
                        }
                        for r in view.reviews
                    ]
                ).astype(str),
                hide_index=True,
                width="stretch",
            )
    tot = trace_totals(view.trace)
    with st.expander(
        f"Workflow trace: {len(view.trace)} steps · {tot['duration_s']:.1f} s · "
        f"{tot['llm_calls']} LLM calls · {tot['cache_hits']} cache hits"
    ):
        df = trace_frame(view.trace)
        st.dataframe(df, hide_index=True, width="stretch")
