from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

# ── Folder locations ──────────────────────────────────────────────────────────

DATA_DIR              = BASE_DIR / "data"
SAMPLE_INVOICES_DIR   = DATA_DIR / "samples" / "invoices"
REFERENCE_DATA_DIR    = DATA_DIR / "reference"
ARCHIVES_DIR          = DATA_DIR / "archives"
OUTPUT_DIR            = DATA_DIR / "output"
PARSED_INVOICES_JSON  = OUTPUT_DIR / "parsed_invoices.json"

# ── Bulk upload limits ────────────────────────────────────────────────────────

# Maximum number of PDF files that can be uploaded in a single batch.
BULK_UPLOAD_MAX_FILES = 500

# Maximum size of each individual PDF file (in megabytes).
BULK_UPLOAD_MAX_FILE_SIZE_MB = 50

# How many invoices are processed at a time during a bulk upload.
# Lower this if the server feels sluggish during large uploads.
BULK_PROCESSING_BATCH_SIZE = 50

# How many invoices can be processed in parallel at once.
# Raise this if you have a fast machine; lower it if uploads crash or freeze.
BULK_PROCESSING_MAX_WORKERS = 16

# ── Django core settings ──────────────────────────────────────────────────────

SECRET_KEY   = "not-so-secret"
DEBUG        = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.invoices",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF    = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "apps" / "invoices" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            # How many seconds to wait if the database is locked before giving up.
            "timeout": 20,
        },
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE     = "America/Denver"
USE_I18N      = True
USE_TZ        = True

STATIC_URL = "static/"
MEDIA_URL  = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Allow large direct-PDF bulk uploads while keeping large file bodies on disk
# instead of in memory as early as possible.
DATA_UPLOAD_MAX_NUMBER_FILES = BULK_UPLOAD_MAX_FILES
DATA_UPLOAD_MAX_MEMORY_SIZE  = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE  = 512 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Suggestion model settings ─────────────────────────────────────────────────
# These values control how the GL code suggestion model scores and ranks
# candidates for each line item. You can tune them here without touching any
# other code. Restart the server after saving changes (make run).
#
# Quick tuning guide:
#   - Suggestions keep matching the invoice GL even when it seems wrong?
#     → Lower INVOICE_GL_PRIOR (try 1.0).
#   - Suggestions feel random or ignore the invoice GL too often?
#     → Raise INVOICE_GL_PRIOR (try 2.0).
#   - You have 200+ approved items and want history to matter more?
#     → Lower HISTORY_MIN_SIMILARITY slightly (try 0.35).
#   - You have 500+ approved items and want less manual review?
#     → Lower AUTO_APPROVE_AGREE (try 0.80).

ML_CONFIG = {

    # ── How much the model's word-matching score counts ───────────────────────

    # The model compares each line item's description against every GL account
    # description and produces a match score from 0 to 1. This multiplier
    # controls how much that score influences the final ranking.
    # Higher = trust word-matching more; lower = trust it less.
    "EMBEDDING_WEIGHT": 4.0,

    # ── How sticky the GL code printed on the invoice is ─────────────────────

    # Every invoice already has a GL code on it. This is a flat bonus added to
    # that code's score. At 1.5, the model needs to be pretty confident in a
    # different code before it will suggest something other than what the invoice
    # already says. Raise this to make the invoice GL even stickier; lower it to
    # let the model override it more freely.
    "INVOICE_GL_PRIOR": 2,

    # ── Small boost for commonly-used GL code ranges ──────────────────────────

    # GL codes whose numeric value falls between REVIEW_RANGE_MIN and
    # REVIEW_RANGE_MAX (inclusive) are treated as the commonly-used expense
    # range and receive a small scoring nudge. Change these two values if your
    # active expense codes live in a different numeric range.
    "REVIEW_RANGE_MIN": 6000,
    "REVIEW_RANGE_MAX": 7070,

    # How large that scoring nudge is. It is intentionally small — the invoice
    # GL and word-matching signals will always dominate it.
    # Set to 0.0 to turn this off entirely.
    "REVIEW_RANGE_WEIGHT": 0.75,

    # ── How many approved past items the model looks back at ──────────────────

    # When you approve a line item, that decision gets saved and used to inform
    # future suggestions. This controls how many of the most similar past
    # approvals are considered when making a new suggestion.
    # Higher = smoother, but less sharp. Lower = sharper, but noisier.
    "KNN_K": 5,

    # Approved past items that are too different from the current line item are
    # ignored. This is the minimum similarity required before a past approval
    # is allowed to influence the suggestion. Items below this threshold are
    # treated as unrelated and skipped.
    # Lower this (e.g. 0.35) after you have 500+ approved items to cast a wider
    # net. Keep it higher early on to avoid noisy results.
    "KNN_MIN_SIMILARITY": 0.45,

    # ── Approval queue tier thresholds ───────────────────────────────────────
    # These control how line items are color-coded in the review queue.
    # Items are placed into one of three lanes:
    #
    # "Auto" (green) — high confidence, suggestion agrees with invoice GL.
    #   These are very likely correct and need only a quick glance.
    #   TIER_AUTO_APPROVE_AGREE sets the confidence cutoff for this lane.
    "TIER_AUTO_APPROVE_AGREE": 0.90,

    # "Auto override" — high confidence even when suggestion differs from invoice GL.
    #   Keep this very high. The model must be near-certain before showing
    #   a different code without a human double-checking.
    "TIER_AUTO_APPROVE_OVERRIDE": 0.95,

    # "Confirm" (yellow) — moderate confidence, suggestion agrees with invoice GL.
    #   Worth a quick look but probably fine.
    "TIER_QUICK_CONFIRM_AGREE": 0.60,

    # "Confirm override" — moderate confidence when suggestion differs from invoice GL.
    "TIER_QUICK_CONFIRM_OVERRIDE": 0.85,

    # Items below all of the above land in the full review lane (red) and
    # must be approved manually.
}
