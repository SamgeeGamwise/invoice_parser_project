VENV = .venv
EMBEDDING_MODEL = sentence-transformers/all-MiniLM-L6-v2

ifeq ($(OS),Windows_NT)
  PYTHON = $(VENV)/Scripts/python
  PIP    = $(VENV)/Scripts/pip
else
  PYTHON = $(VENV)/bin/python
  PIP    = $(VENV)/bin/pip
endif

MANAGE = $(PYTHON) manage.py

# ── Run ──────────────────────────────────────────────────────────────────────

run:           ## Start the development server (http://127.0.0.1:8000)
	$(MANAGE) runserver

# ── Database ─────────────────────────────────────────────────────────────────

migrate:       ## Apply pending database migrations
	$(MANAGE) migrate

migrations:    ## Generate new migrations after model changes
	$(MANAGE) makemigrations

clear-invoices: ## Wipe invoice and line-item data only
	$(MANAGE) clear_data --yes

clear-codes:   ## Wipe GL codes and property references only
	$(MANAGE) clear_data --yes --codes-only

clear-history: ## Clear all GL approvals (resets KNN history) while keeping invoices and reference data
	$(PYTHON) -c "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup(); from apps.invoices.models import InvoiceLineItem; n = InvoiceLineItem.objects.filter(approved_gl__isnull=False).update(approved_gl=None, reviewed_at=None); print(f'Cleared {n} approval(s).')"

cache-model:   ## Download/cache the sentence-transformer model used for GL suggestions
	$(PYTHON) -c "import os; os.environ['INVOICE_PARSER_ALLOW_MODEL_DOWNLOAD']='1'; from sentence_transformers import SentenceTransformer; SentenceTransformer('$(EMBEDDING_MODEL)'); print('Cached $(EMBEDDING_MODEL)')"

# ── Setup ────────────────────────────────────────────────────────────────────

install:       ## Create venv and install dependencies
	python -m venv $(VENV)
	$(PIP) install -r requirements.txt

setup: install migrate ## First-time setup (import GL codes and properties via the UI)

# ── Help ─────────────────────────────────────────────────────────────────────

help:          ## List all available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: run migrate migrations clear-invoices clear-codes clear-history cache-model install setup help
.DEFAULT_GOAL := help
