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

reset:         ## Wipe all invoice data (keeps GL accounts and property refs)
	$(MANAGE) clear_data --yes

# ── Setup ────────────────────────────────────────────────────────────────────

install:       ## Create venv and install dependencies
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

setup: install migrate  ## Full first-time setup

# ── Help ─────────────────────────────────────────────────────────────────────

help:          ## List all available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: run migrate migrations reset install setup help
.DEFAULT_GOAL := help
