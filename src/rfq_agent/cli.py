"""`rfq` command line: run an RFQ through the graph, review it, inspect runs, search the knowledge base."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from rfq_agent import app_api
from rfq_agent.config import BIZ
from rfq_agent.graph.edits import EditError
from rfq_agent.models import ReviewDecision, ReviewEdit

app = typer.Typer(add_completion=False, no_args_is_help=True, help="AI RFQ & Quotation Agent (prototype)")
console = Console()

TIER_STYLE = {
    "fast_track": "bold white on green",
    "standard": "bold black on yellow",
    "manual": "bold white on red",
}
SEV_STYLE = {"blocker": "bold red", "warning": "yellow", "info": "cyan"}


def _badge(tier: str) -> Text:
    return Text(f" {tier.upper()} ", style=TIER_STYLE.get(tier, "bold"))


def _money(x: float) -> str:
    return f"{x:,.2f}"


def print_line(ln, raw: dict) -> None:
    conf = ln.confidence
    head = Text.assemble(
        f"Line {ln.line_no}  ",
        (ln.spec.title_block.part_number or "?", "bold"),
        f"  {ln.spec.title_block.title or ''}  ",
        _badge(conf.tier.value),
        f"  confidence {conf.overall:.2f} (extraction {conf.extraction:.2f} · similarity {conf.similarity:.2f}"
        f" · coverage {conf.coverage:.2f} · price {conf.price_sanity:.2f}"
        + (f" · BOM {conf.bom_match:.2f}" if conf.bom_match is not None else "")
        + ")",
    )
    console.print(head)
    for r in conf.reasons:
        console.print(f"   • {r}")
    if ln.issues:
        t = Table(title="Issues", title_justify="left", show_edge=False, pad_edge=False)
        for c in ("sev", "code", "message", "kb"):
            t.add_column(c)
        for i in ln.issues:
            t.add_row(
                Text(i.severity.value, style=SEV_STYLE[i.severity.value]), i.code, i.message, i.kb_ref or ""
            )
        console.print(t)
    if ln.similar_parts:
        console.print(
            "   similar: "
            + ", ".join(f"{s.part_number} ({s.family}) {s.score:.2f}" for s in ln.similar_parts)
        )
    rt = Table(title="Routing", title_justify="left", show_edge=False, pad_edge=False)
    for c in ("seq", "op", "work center", "setup min", "cycle min", "basis", "note"):
        rt.add_column(c, justify="right" if "min" in c or c == "seq" else "left")
    for op in ln.routing:
        note = "suggested, not costed" if op.suggested else ("outsourced" if op.outsourced else "")
        style = "dim" if op.suggested else ("bold magenta" if op.basis == "manual" else "")
        rt.add_row(
            str(op.seq),
            op.op_code,
            op.work_center + (f" ({op.service_code})" if op.service_code else ""),
            f"{op.setup_min:g}",
            f"{op.cycle_min:g}",
            op.basis,
            note,
            style=style,
        )
    console.print(rt)
    qtys = raw.get("quantities") or sorted({c.qty for c in ln.costs})
    options = set(raw.get("option_quantities", []))
    ct = Table(title="Unit price EUR by plant", title_justify="left", show_edge=False, pad_edge=False)
    ct.add_column("qty", justify="right")
    for p in BIZ.plants:
        mark = " ★" if p == ln.recommended_plant else ""
        ct.add_column(p + mark, justify="right")
    ct.add_column("lead (wk)", justify="right")
    for q in qtys:
        row = [f"{q:,}" + (" (opt)" if q in options else "")]
        for p in BIZ.plants:
            c = next((c for c in ln.costs if c.plant == p and c.qty == q), None)
            if c is None:
                row.append("-")
            elif not c.feasible:
                row.append(Text("✗ " + (c.infeasible_reason or ""), style="red"))
            else:
                cell = _money(c.unit_price_eur) + (" (MOV)" if c.min_order_applied else "")
                if c.meets_due_date is False:
                    cell += " late"
                row.append(Text(cell, style="bold green" if p == ln.recommended_plant else ""))
        best = next((c for c in ln.costs if c.plant == ln.recommended_plant and c.qty == q), None)
        row.append(str(best.lead_time_weeks) if best else "-")
        ct.add_row(*row)
    console.print(ct)
    over = " (reviewer override)" if raw.get("plant_overridden") else ""
    dev = (
        f", deviation vs. reference {ln.price_deviation_pct:+.0%}"
        if ln.price_deviation_pct is not None
        else ""
    )
    console.print(f"   recommended plant: [bold]{ln.recommended_plant or 'none feasible'}[/bold]{over}{dev}")


def print_view(view: app_api.RunView, *, trace: bool = False) -> None:
    req = view.request
    title = f"{view.rfq_id} · status [bold]{view.status}[/bold]"
    if req:
        title += f" · {req.customer_name} ({req.language}) · extractor {view.extractor}"
    console.print(Panel.fit(title, border_style="blue"))
    if view.error:
        console.print(f"[red]ERROR:[/red] {view.error}")
    if view.draft:
        raw = {ln["line_no"]: ln for ln in view.lines}
        console.print(f"margin {view.draft.margin_pct:.0%} · draft v{view.draft.version}")
        for ln in view.draft.lines:
            print_line(ln, raw.get(ln.line_no, {}))
            console.print()
    elif view.issues:
        for i in view.issues:
            console.print(
                Text(
                    f"  {i.severity.value.upper():8} {i.code}  {i.message}", style=SEV_STYLE[i.severity.value]
                )
            )
    if view.clarification:
        c = view.clarification
        console.print(
            Panel(
                f"To: {c.get('to') or '-'}\nSubject: {c['subject']}\n\n{c['body']}",
                title=f"Clarification e-mail ({c['language']}, {c['source']}) — no price issued",
                border_style="yellow",
            )
        )
    if view.review_error:
        console.print(f"[red]Last edit rejected:[/red] {view.review_error}")
    if view.quote:
        q = view.quote
        who = f"{q.approved_by}" + (" [auto]" if view.auto_approved else "")
        console.print(Panel(q.cover_letter, title=f"{q.quote_id} — approved by {who}", border_style="green"))
        if view.quote_paths:
            console.print(f"quote HTML: {view.quote_paths['html']}\nquote JSON: {view.quote_paths['json']}")
    if view.pending_review:
        console.print(
            f"[yellow]Waiting for engineer review.[/yellow] e.g.\n"
            f"  rfq review {view.rfq_id} --approve --reviewer NAME\n"
            f"  rfq review {view.rfq_id} --edit-cycle LINE:SEQ:MIN --reviewer NAME\n"
            f"  rfq review {view.rfq_id} --margin 20 | --plant LINE:PL | --reject | --clarify"
        )
    if trace and view.trace:
        t = Table(title="Trace", title_justify="left", show_edge=False, pad_edge=False)
        for c in ("node", "ms", "llm", "cache", "tok in/out", "summary"):
            t.add_column(c)
        for e in view.trace:
            t.add_row(
                e.node,
                str(e.duration_ms),
                str(e.llm_calls),
                str(e.cache_hits),
                f"{e.input_tokens}/{e.output_tokens}",
                e.summary,
            )
        console.print(t)


@app.command()
def run(
    rfq_dir: Annotated[Path, typer.Argument(help="RFQ folder with email.txt and drawings/")],
    auto_approve: Annotated[
        bool, typer.Option(help="Approve automatically if every line is FAST_TRACK")
    ] = False,
    extractor: Annotated[
        str,
        typer.Option(help="llm (model, record/replay) | reference (gold data, for testing without a model)"),
    ] = "llm",
) -> None:
    """Run an RFQ through the workflow until review, clarification or quote."""
    if extractor not in ("llm", "reference"):
        raise typer.BadParameter("extractor must be 'llm' or 'reference'")
    if extractor == "reference":
        console.print("[dim]extractor=reference: using hand-written reference data, no model involved[/dim]")
    view = app_api.start(rfq_dir, extractor, auto_approve=auto_approve)
    print_view(view, trace=True)
    if auto_approve and view.pending_review:
        console.print("[yellow]--auto-approve only applies to FAST_TRACK lines; left for review.[/yellow]")


def _split(spec: str, n: int, what: str) -> list[str]:
    parts = spec.split(":")
    if len(parts) != n:
        raise typer.BadParameter(f"{what} expects {n} ':'-separated values, got {spec!r}")
    return parts


@app.command()
def review(
    rfq_id: str,
    approve: bool = False,
    edit_cycle: Annotated[list[str], typer.Option(help="LINE:SEQ:MIN cycle time")] = [],  # noqa: B006
    edit_setup: Annotated[list[str], typer.Option(help="LINE:SEQ:MIN setup time")] = [],  # noqa: B006
    accept: Annotated[list[str], typer.Option(help="LINE:SEQ accept a suggested op")] = [],  # noqa: B006
    remove_op: Annotated[list[str], typer.Option(help="LINE:SEQ remove an op")] = [],  # noqa: B006
    plant: Annotated[list[str], typer.Option(help="LINE:PLANT override (DE/PL/CN/auto)")] = [],  # noqa: B006
    margin: Annotated[float | None, typer.Option(help="Target margin in percent, e.g. 20")] = None,
    reject: bool = False,
    clarify: Annotated[bool, typer.Option(help="Ask the customer instead of quoting")] = False,
    comment: str | None = None,
    reviewer: str = "engineer",
) -> None:
    """Approve, edit (then re-review), reject or send back for clarification."""
    edits: list[ReviewEdit] = []
    for s in edit_cycle:
        ln, seq, v = _split(s, 3, "--edit-cycle")
        edits.append(
            ReviewEdit(line_no=int(ln), target="routing", seq=int(seq), field="cycle_min", new_value=float(v))
        )
    for s in edit_setup:
        ln, seq, v = _split(s, 3, "--edit-setup")
        edits.append(
            ReviewEdit(line_no=int(ln), target="routing", seq=int(seq), field="setup_min", new_value=float(v))
        )
    for s in accept:
        ln, seq = _split(s, 2, "--accept")
        edits.append(ReviewEdit(line_no=int(ln), target="routing", seq=int(seq), field="accept_suggested"))
    for s in remove_op:
        ln, seq = _split(s, 2, "--remove-op")
        edits.append(ReviewEdit(line_no=int(ln), target="routing", seq=int(seq), field="remove_op"))
    for s in plant:
        ln, p = _split(s, 2, "--plant")
        edits.append(ReviewEdit(line_no=int(ln), target="plant", field="plant", new_value=p))
    if margin is not None:
        edits.append(ReviewEdit(line_no=1, target="margin", field="margin_pct", new_value=margin))
    actions = [
        a for a, on in (("approve", approve), ("reject", reject), ("request_clarification", clarify)) if on
    ]
    if edits:
        actions.append("edit")
    if len(actions) != 1:
        raise typer.BadParameter("give exactly one of --approve / edits / --reject / --clarify")
    decision = ReviewDecision(action=actions[0], reviewer=reviewer, edits=edits, comment=comment)
    try:
        view = app_api.review(rfq_id, decision)
    except (EditError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    print_view(view)


@app.command()
def show(rfq_id: str, trace: bool = True) -> None:
    """Show the current state of a run (draft, clarification or quote) and its trace."""
    view = app_api.get(rfq_id)
    if view is None:
        console.print(f"[red]No run for {rfq_id}[/red]")
        raise typer.Exit(1)
    print_view(view, trace=trace)


@app.command()
def runs() -> None:
    """List all runs in the checkpoint store."""
    t = Table("rfq", "status", "pending review", "customer", "tiers", "quote")
    for r in app_api.list_runs():
        t.add_row(
            r["rfq_id"],
            r["status"],
            "yes" if r["pending_review"] else "",
            r["customer"] or "",
            ", ".join(f"L{k}:{v}" for k, v in r["tiers"].items()),
            r["quote_id"] or "",
        )
    console.print(t)


@app.command()
def kb(query: str, k: int = 4) -> None:
    """Search the engineering knowledge base."""
    for hit in app_api.kb_search(query, k):
        console.print(
            Panel(hit["text"].strip()[:900], title=f"{hit['id']}  ({hit['score']:.2f})", title_align="left")
        )


@app.command()
def similar(
    path: Annotated[
        Path, typer.Argument(help="DrawingSpec JSON (e.g. expected/FL-2208.json) or a drawing PDF")
    ],
    qty: int = 200,
) -> None:
    """Find the most similar historical parts for a drawing."""
    hits = app_api.similar(path, qty)
    t = Table("part", "title", "family", "score", "breakdown", "ref cost €/pc (qty, plant)")
    for s in hits:
        bd = " ".join(f"{k}={v:.2f}" for k, v in s.score_breakdown.items())
        ref = (
            f"{s.ref_unit_cost_eur} ({s.ref_qty}, {s.ref_plant})" if s.ref_unit_cost_eur is not None else "-"
        )
        t.add_row(s.part_number, s.title, s.family, f"{s.score:.2f}", bd, ref)
    console.print(t if hits else "[yellow]no similar part above the minimum score[/yellow]")


if __name__ == "__main__":
    app()
