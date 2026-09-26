"""Graph nodes (DESIGN §4.2). Each node reads JSON state, calls the deterministic modules, returns an update.

Multi-line RFQs are handled by looping over lines inside each node (production: LangGraph `Send`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from langgraph.types import interrupt

from rfq_agent.agents import quote_writer
from rfq_agent.agents.bom import build_bom
from rfq_agent.agents.confidence import assess
from rfq_agent.agents.costing import compute_costs, price_deviation, recommend_plant
from rfq_agent.agents.geometry import envelope_volume_cm3, finished_volume_cm3, weight_kg
from rfq_agent.agents.postprocess import postprocess
from rfq_agent.agents.review_rules import review_line
from rfq_agent.agents.routing_rules import plan_routing
from rfq_agent.agents.similarity import find_similar, reuse_hint
from rfq_agent.config import BIZ
from rfq_agent.graph.deps import Deps
from rfq_agent.graph.edits import EditError, apply_edits, diff, snapshot
from rfq_agent.graph.state import RFQState
from rfq_agent.models import (
    BOMLine,
    CostBreakdown,
    DrawingSpec,
    LineQuoteDraft,
    QuoteDraft,
    ReviewDecision,
    RFQItem,
    RFQRequest,
    RFQStatus,
    RoutingOp,
    Severity,
    SimilarPart,
    TraceEvent,
    ValidationIssue,
)


def dump(m) -> dict:
    return m.model_dump(mode="json")


def dumps(ms) -> list[dict]:
    return [dump(m) for m in ms]


def _stats(deps: Deps) -> dict[str, int]:
    tot = {"llm_calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
    for c in deps.clients():
        for k in tot:
            tot[k] += int(c.stats.get(k, 0))
    return tot


def traced(name: str, fn: Callable[[RFQState], dict], deps: Deps) -> Callable[[RFQState], dict]:
    """Wrap a node: append a TraceEvent (duration, LLM calls, cache hits, tokens, one-line summary)."""

    def run(state: RFQState) -> dict:
        started = datetime.now()
        t0 = time.perf_counter()
        s0 = _stats(deps)
        out = fn(state)
        s1 = _stats(deps)
        ev = TraceEvent(
            node=name,
            started_at=started,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            llm_calls=s1["llm_calls"] - s0["llm_calls"],
            cache_hits=s1["cache_hits"] - s0["cache_hits"],
            input_tokens=s1["input_tokens"] - s0["input_tokens"],
            output_tokens=s1["output_tokens"] - s0["output_tokens"],
            summary=out.pop("_summary", ""),
        )
        out["trace"] = [dump(ev)]
        return out

    run.__name__ = name
    return run


def _line_models(line: dict) -> dict:
    """Typed view of a line dict (only the keys present)."""
    conv = {
        "item": RFQItem.model_validate,
        "spec": lambda d: DrawingSpec.model_validate(d) if d else None,
        "issues": lambda xs: [ValidationIssue.model_validate(x) for x in xs],
        "similar_parts": lambda xs: [SimilarPart.model_validate(x) for x in xs],
        "bom": lambda xs: [BOMLine.model_validate(x) for x in xs],
        "routing": lambda xs: [RoutingOp.model_validate(x) for x in xs],
        "costs": lambda xs: [CostBreakdown.model_validate(x) for x in xs],
    }
    return {k: conv[k](v) if k in conv else v for k, v in line.items()}


def _pn(line: dict) -> str:
    spec = line.get("spec") or {}
    return (
        (spec.get("title_block") or {}).get("part_number") or line["item"].get("customer_part_number") or "?"
    )


def make_nodes(deps: Deps) -> dict[str, Callable[[RFQState], dict]]:
    mats, parts = deps.materials, deps.parts

    # ---------- intake ----------
    def intake(state: RFQState) -> dict:
        rfq_dir = Path(state["rfq_dir"])
        ex = deps.extractor(state.get("extractor", "llm"))
        try:
            req = ex.parse_rfq(rfq_dir)
        except Exception as e:
            return {
                "status": RFQStatus.ERROR.value,
                "error": f"intake failed: {type(e).__name__}: {e}",
                "_summary": f"ERROR {type(e).__name__}",
            }
        return {
            "request": dump(req),
            "status": RFQStatus.NEW.value,
            "_summary": (
                f"{req.customer_name} ({req.language}), {len(req.items)} line(s), extractor={ex.name}"
            ),
        }

    # ---------- extract_drawings ----------
    def extract_drawings(state: RFQState) -> dict:
        req = RFQRequest.model_validate(state["request"])
        rfq_dir = Path(state["rfq_dir"])
        ex = deps.extractor(state.get("extractor", "llm"))
        lines, notes = [], []
        for item in req.items:
            pdf = rfq_dir / "drawings" / item.drawing_ref
            line = {"line_no": item.line_no, "item": dump(item), "spec": None, "render_paths": []}
            if not pdf.exists():
                notes.append(f"L{item.line_no}: drawing '{item.drawing_ref}' missing")
            else:
                try:
                    spec, renders = ex.extract(pdf, rfq_id=req.rfq_id)
                    spec = postprocess(spec, mats)  # always, whatever the extractor
                    spec.extraction_warnings = list(dict.fromkeys(spec.extraction_warnings))
                    line["spec"] = dump(spec)
                    line["render_paths"] = [str(p) for p in renders]
                    notes.append(
                        f"L{item.line_no}: {spec.title_block.part_number} {spec.envelope.shape_class.value}, "
                        f"{len(spec.features)} features"
                    )
                except Exception as e:
                    notes.append(f"L{item.line_no}: extraction failed ({type(e).__name__})")
                    line["extraction_error"] = f"{type(e).__name__}: {e}"
            lines.append(line)
        return {"lines": lines, "status": RFQStatus.EXTRACTED.value, "_summary": "; ".join(notes)}

    # ---------- validate ----------
    def validate(state: RFQState) -> dict:
        lines = [dict(ln) for ln in state["lines"]]
        all_issues = []
        for ln in lines:
            m = _line_models(ln)
            issues = review_line(
                line_no=ln["line_no"], item=m["item"], spec=m["spec"], materials=mats, parts=parts
            )
            if ln.get("extraction_error") and not any(i.severity == Severity.BLOCKER for i in issues):
                issues.insert(
                    0,
                    ValidationIssue(
                        code="VAL-010",
                        severity=Severity.BLOCKER,
                        line_no=ln["line_no"],
                        field_path="drawing_ref",
                        message=f"Drawing for line {ln['line_no']} could not be read automatically.",
                        suggestion="Check the drawing manually or ask the customer for a clean PDF.",
                        kb_ref="quoting_policy.md#rfq-completeness",
                    ),
                )
            ln["issues"] = dumps(issues)
            all_issues += ln["issues"]
        n = {s: sum(i["severity"] == s.value for i in all_issues) for s in Severity}
        codes = ",".join(dict.fromkeys(i["code"] for i in all_issues)) or "-"
        return {
            "lines": lines,
            "issues": all_issues,
            "_summary": f"{n[Severity.BLOCKER]} blocker / {n[Severity.WARNING]} warning / "
            f"{n[Severity.INFO]} info ({codes})",
        }

    # ---------- clarify ----------
    def clarify(state: RFQState) -> dict:
        req = RFQRequest.model_validate(state["request"])
        issues = [ValidationIssue.model_validate(i) for i in state.get("issues", [])]
        review = state.get("review") or {}
        comment = review.get("comment") if review.get("action") == "request_clarification" else None
        mail = quote_writer.clarification_email(req, issues, deps.client, comment=comment)
        return {
            "clarification": mail,
            "clarification_email": mail["body"],
            "status": RFQStatus.NEEDS_CLARIFICATION.value,
            "_summary": f"{len(mail['questions'])} question(s), {mail['language']}, source={mail['source']}",
        }

    # ---------- retrieve_similar ----------
    def retrieve_similar(state: RFQState) -> dict:
        lines = [dict(ln) for ln in state["lines"]]
        notes = []
        added: list[dict] = []
        for ln in lines:
            m = _line_models(ln)
            qty = min(m["item"].quantities) if m["item"].quantities else 200
            sims = find_similar(m["spec"], parts, mats, qty=qty)
            ln["similar_parts"] = dumps(sims)
            if hint := reuse_hint(m["spec"], sims, ln["line_no"]):
                ln["issues"] = ln["issues"] + [dump(hint)]
                added.append(dump(hint))
            top = f"{sims[0].part_number} {sims[0].score:.2f}" if sims else "none"
            notes.append(f"L{ln['line_no']}: top {top}" + (" + REUSE-001" if added else ""))
        return {"lines": lines, "issues": state.get("issues", []) + added, "_summary": "; ".join(notes)}

    # ---------- plan_bom_routing ----------
    def plan_bom_routing(state: RFQState) -> dict:
        lines = [dict(ln) for ln in state["lines"]]
        notes = []
        for ln in lines:
            m = _line_models(ln)
            spec = m["spec"]
            mat = mats.all.get(spec.title_block.material_code or "")
            routing, uncovered = plan_routing(spec, mat, deps.services, m["similar_parts"], parts)
            bom = build_bom(spec, mat, routing, deps.catalog, parts)
            ln["routing"], ln["uncovered_features"], ln["bom"] = dumps(routing), uncovered, dumps(bom)
            ops = "→".join(op.op_code for op in routing if not op.suggested)
            sugg = [op.op_code for op in routing if op.suggested]
            notes.append(
                f"L{ln['line_no']}: {ops}"
                + (f" (+suggested {','.join(sugg)})" if sugg else "")
                + f", BOM {len(bom)} rows"
            )
        return {"lines": lines, "_summary": "; ".join(notes)}

    # ---------- cost ----------
    def cost(state: RFQState) -> dict:
        lines = [dict(ln) for ln in state["lines"]]
        margin = state.get("margin_pct", BIZ.margin_pct)
        overrides = {o["line_no"]: o["plant"] for o in state.get("plant_override", [])}
        notes = []
        for ln in lines:
            m = _line_models(ln)
            spec, item = m["spec"], m["item"]
            mat = mats.all.get(spec.title_block.material_code or "")
            fin = weight_kg(finished_volume_cm3(spec), mat.density_g_cm3) if mat else 0.0
            qtys, options = quote_writer.offer_quantities(item.quantities)
            costs = compute_costs(
                routing=m["routing"],
                bom=m["bom"],
                quantities=qtys,
                finished_weight_kg=fin,
                requested_delivery=item.requested_delivery,
                today=deps.today_or_now(),
                rates=deps.rates,
                services=deps.services,
                margin_pct=margin,
            )
            primary = qtys[0] if qtys else 0
            plant = overrides.get(ln["line_no"]) or recommend_plant(costs, primary)
            dev = price_deviation(costs, plant, primary, m["similar_parts"], envelope_volume_cm3(spec), parts)
            ln.update(
                costs=dumps(costs),
                quantities=qtys,
                option_quantities=sorted(options),
                recommended_plant=plant,
                plant_overridden=ln["line_no"] in overrides,
                price_deviation_pct=dev,
            )
            best = next((c for c in costs if c.plant == plant and c.qty == primary), None)
            price = f"{best.unit_price_eur:.2f} €/pc @ {primary}" if best else "no feasible plant"
            infeasible = sorted({c.plant for c in costs if not c.feasible})
            notes.append(
                f"L{ln['line_no']}: {plant} {price}"
                + (f", infeasible {','.join(infeasible)}" if infeasible else "")
                + (f", dev {dev:+.0%}" if dev is not None else "")
            )
        return {"lines": lines, "margin_pct": margin, "_summary": "; ".join(notes)}

    # ---------- assess ----------
    def assess_node(state: RFQState) -> dict:
        lines = [dict(ln) for ln in state["lines"]]
        req = RFQRequest.model_validate(state["request"])
        drafts, notes = [], []
        for ln in lines:
            m = _line_models(ln)
            conf = assess(
                spec=m["spec"],
                item=m["item"],
                issues=m["issues"],
                similar=m["similar_parts"],
                routing=m["routing"],
                uncovered=ln["uncovered_features"],
                bom=m["bom"],
                price_deviation_pct=ln["price_deviation_pct"],
            )
            ln["confidence"] = dump(conf)
            drafts.append(
                LineQuoteDraft(
                    line_no=ln["line_no"],
                    item=m["item"],
                    spec=m["spec"],
                    render_paths=ln.get("render_paths", []),
                    issues=m["issues"],
                    similar_parts=m["similar_parts"],
                    bom=m["bom"],
                    routing=m["routing"],
                    uncovered_features=ln["uncovered_features"],
                    costs=m["costs"],
                    recommended_plant=ln["recommended_plant"],
                    price_deviation_pct=ln["price_deviation_pct"],
                    confidence=conf,
                )
            )
            notes.append(f"L{ln['line_no']}: {conf.tier.value.upper()} {conf.overall:.2f}")
        options = {ln["line_no"]: set(ln.get("option_quantities", [])) for ln in lines}
        margin = state.get("margin_pct", BIZ.margin_pct)
        draft = QuoteDraft(
            rfq_id=state["rfq_id"],
            version=state.get("review_round", 0) + 1,
            request=req,
            lines=drafts,
            assumptions=quote_writer.build_assumptions(req, drafts, mats, options),
            margin_pct=margin,
        )
        out = {
            "lines": lines,
            "draft": dump(draft),
            "status": RFQStatus.IN_REVIEW.value,
            "_summary": "; ".join(notes),
        }
        if pending := state.get("pending_feedback"):
            # an edit round is complete once costs and confidence are recomputed: record before/after
            _write_feedback(pending, lines, margin)
            out["pending_feedback"] = None
            out["_summary"] += f"; feedback recorded for line(s) {[b['line_no'] for b in pending['before']]}"
        return out

    def _write_feedback(pending: dict, lines: list[dict], margin: float) -> None:
        by_no = {ln["line_no"]: ln for ln in lines}
        for before in pending["before"]:
            line_no = before["line_no"]
            after = snapshot(by_no[line_no], margin)
            edits = [e for e in pending["edits"] if e["line_no"] == line_no]
            changes = diff(before["snapshot"], after)
            deps.feedback.add(
                pending["rfq_id"],
                line_no,
                pending["reviewer"],
                json.dumps(edits, ensure_ascii=False),
                json.dumps(before["snapshot"], ensure_ascii=False),
                json.dumps({**after, "diff": changes, "review_round": pending["round"]}, ensure_ascii=False),
            )

    # ---------- human_review ----------
    def human_review(state: RFQState) -> dict:
        payload = {
            "rfq_id": state["rfq_id"],
            "review_round": state.get("review_round", 0),
            "tiers": {str(ln["line_no"]): ln["confidence"]["tier"] for ln in state["lines"]},
            "review_error": state.get("review_error"),
            "draft": state["draft"],
        }
        raw = interrupt(payload)  # side-effect free above this line: the node re-runs on resume
        decision = raw if isinstance(raw, ReviewDecision) else ReviewDecision.model_validate(raw)
        rec = dump(decision) | {"at": datetime.now().isoformat(timespec="seconds")}
        out: dict = {"review": dump(decision), "reviews": [rec], "review_error": None}
        summary = f"{decision.action} by {decision.reviewer}"
        if decision.action == "edit":
            try:
                lines, margin, overrides = apply_edits(
                    state["lines"],
                    state.get("margin_pct", BIZ.margin_pct),
                    state.get("plant_override", []),
                    decision.edits,
                )
            except EditError as e:
                out["review_error"] = str(e)
                out["review"] = dump(decision) | {"action": "invalid_edit"}
                return out | {"_summary": f"edit rejected: {e}"}
            margin_before = state.get("margin_pct", BIZ.margin_pct)
            touched = sorted({e.line_no for e in decision.edits})
            by_no = {ln["line_no"]: ln for ln in state["lines"]}
            out |= {
                "lines": lines,
                "margin_pct": margin,
                "plant_override": overrides,
                "review_round": state.get("review_round", 0) + 1,
                "pending_feedback": {
                    "rfq_id": state["rfq_id"],
                    "reviewer": decision.reviewer,
                    "round": state.get("review_round", 0) + 1,
                    "edits": [dump(e) for e in decision.edits],
                    "before": [
                        {"line_no": n, "snapshot": snapshot(by_no[n], margin_before)}
                        for n in touched
                        if n in by_no
                    ],
                },
            }
            summary += f", {len(decision.edits)} edit(s) on line(s) {touched}"
        elif decision.action == "reject":
            out["status"] = RFQStatus.REJECTED.value
        return out | {"_summary": summary}

    # ---------- finalize ----------
    def finalize(state: RFQState) -> dict:
        draft = QuoteDraft.model_validate(state["draft"])
        review = state.get("review") or {}
        quote, paths = quote_writer.write_quote(
            draft,
            approved_by=review.get("reviewer", "unknown"),
            client=deps.client,
            quote_repo=deps.quotes,
            quote_dir=deps.quote_dir,
            today=deps.today_or_now(),
            comment=review.get("comment"),
        )
        return {
            "quote": dump(quote),
            "quote_paths": paths,
            "status": RFQStatus.APPROVED.value,
            "_summary": f"{quote.quote_id}, cover letter source={quote.cover_letter_source}",
        }

    return {
        "intake": intake,
        "extract_drawings": extract_drawings,
        "validate": validate,
        "clarify": clarify,
        "retrieve_similar": retrieve_similar,
        "plan_bom_routing": plan_bom_routing,
        "cost": cost,
        "assess": assess_node,
        "human_review": human_review,
        "finalize": finalize,
    }
