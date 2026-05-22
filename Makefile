TICKER  ?= PANW
PYTHON  := .venv/bin/python
PIP     := .venv/bin/pip

# Load .env if present so API keys are available to all targets
-include .env
export

.PHONY: setup ingest warehouse model forecast dashboard commentary \
        notebooklm test eval lint typecheck qa demo demo-quick clean

# ── Environment ───────────────────────────────────────────────────────────────
setup:
	uv venv --python 3.11 .venv
	uv pip install --python .venv/bin/python -r requirements.txt
	@echo ""
	@echo "NOTE: First 'make forecast' triggers cmdstan download (~200 MB, ~5 min)."
	@echo "Pre-stage with:  $(PYTHON) -c 'import cmdstanpy; cmdstanpy.install_cmdstan()'"

# ── Pipeline steps (run in order) ─────────────────────────────────────────────
ingest:
	$(PYTHON) -m src.ingest_edgar --ticker $(TICKER)

warehouse:
	$(PYTHON) -m src.build_warehouse --ticker $(TICKER)

model:
	$(PYTHON) -m papermill notebooks/02_baseline_forecast.ipynb /dev/null \
		-p ticker $(TICKER)

forecast:
	$(PYTHON) -m papermill notebooks/03_macro_regularized_forecast.ipynb /dev/null \
		-p ticker $(TICKER)
	$(PYTHON) -m src.build_variance_facts --ticker $(TICKER)

dashboard:
	$(PYTHON) -m src.build_excel_model --ticker $(TICKER)
	$(PYTHON) -m src.export_for_tableau --ticker $(TICKER)

# Pass LIVE=1 to call the Anthropic API; default is --dry-run (prints prompt, no cost).
# Examples:
#   make commentary TICKER=PANW          # dry-run
#   make commentary TICKER=PANW LIVE=1   # calls API
commentary:
	$(PYTHON) -m src.generate_commentary $(if $(LIVE),--live,--dry-run) --ticker $(TICKER)

notebooklm:
	$(PYTHON) -m src.build_notebooklm_bundle --ticker $(TICKER)

# ── Quality gates ─────────────────────────────────────────────────────────────
test:
	$(PYTHON) -m pytest tests/ -v

# Eval runs only the ground-truth scenario tests; --no-cov avoids the 60%
# threshold that applies to the full src/ coverage check.
eval:
	$(PYTHON) -m pytest tests/eval/ -v --no-cov

lint:
	$(PYTHON) -m ruff check src/ tests/
	$(PYTHON) -m ruff format --check src/ tests/

typecheck:
	$(PYTHON) -m mypy --strict src/

qa: lint typecheck test eval

# ── End-to-end demos ──────────────────────────────────────────────────────────
# Full demo (requires FRED_API_KEY + ANTHROPIC_API_KEY; cmdstan ~5 min first run).
# Defaults to PANW. Switch ticker: make demo TICKER=CRWD
demo:
	@echo ">>> Full end-to-end demo: $(TICKER)"
	$(MAKE) ingest     TICKER=$(TICKER)
	$(MAKE) warehouse  TICKER=$(TICKER)
	$(MAKE) model      TICKER=$(TICKER)
	$(MAKE) forecast   TICKER=$(TICKER)
	$(MAKE) dashboard  TICKER=$(TICKER)
	$(MAKE) commentary TICKER=$(TICKER)
	$(MAKE) notebooklm TICKER=$(TICKER)
	$(MAKE) test
	$(MAKE) eval

# Quick demo: ingest → warehouse → Excel model only (no forecasts, no API keys).
# Safe to run as a first check after cloning.
demo-quick:
	@echo ">>> Quick demo (no API keys needed): $(TICKER)"
	$(MAKE) ingest    TICKER=$(TICKER)
	$(MAKE) warehouse TICKER=$(TICKER)
	$(MAKE) dashboard TICKER=$(TICKER)
	$(MAKE) test

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean:
	rm -rf .venv __pycache__ .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} +
