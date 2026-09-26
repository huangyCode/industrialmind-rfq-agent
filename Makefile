.PHONY: setup data test lint demo ui eval

setup:
	uv sync

data:
	uv run python scripts/build_db.py

test:
	uv run pytest -q

lint:
	uv run ruff check src scripts tests eval
	uv run ruff format --check src scripts tests eval

demo:
	uv run rfq run data/samples/RFQ-2026-0102 --auto-approve

ui:
	uv run streamlit run src/rfq_agent/ui/app.py

# All reports under eval/reports/. Offline: the extraction eval replays fixtures/llm_cache (skipped if absent).
eval:
	@if [ -f eval/eval_extraction.py ]; then uv run python eval/eval_extraction.py --mode replay; \
	else echo "eval/eval_extraction.py not found - skipping extraction eval"; fi
	uv run python eval/backtest_costing.py
	uv run python eval/eval_routing.py
	uv run python eval/feedback_report.py
