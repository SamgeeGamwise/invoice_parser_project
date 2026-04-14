VENV = .venv
PYTHON = $(VENV)/bin/python
MANAGE = $(PYTHON) manage.py

# ── Run ──────────────────────────────────────────────────────────────────────

run:           ## Start the development server (http://127.0.0.1:8000)
	$(MANAGE) runserver

# ── Database ─────────────────────────────────────────────────────────────────

migrate:       ## Apply pending database migrations
	$(MANAGE) migrate

migrations:    ## Generate new migrations after model changes
	$(MANAGE) makemigrations

import-reference-data: ## Import GL accounts and property references from the Excel files into the DB
	$(MANAGE) import_reference_data

reset:         ## Wipe all data including GL codes and property references (debug reset)
	$(MANAGE) clear_data --yes --all

# ── Setup ────────────────────────────────────────────────────────────────────

install:       ## Create venv and install dependencies
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

setup: install migrate ## First-time setup (import GL codes and properties via the UI)

# ── Help ─────────────────────────────────────────────────────────────────────

help:          ## List all available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: run migrate migrations import-reference-data reset install setup help
.DEFAULT_GOAL := help
