from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SAMPLE_INVOICES_DIR = DATA_DIR / "samples" / "invoices"
REFERENCE_DATA_DIR = DATA_DIR / "reference"
ARCHIVES_DIR = DATA_DIR / "archives"
OUTPUT_DIR = DATA_DIR / "output"
PARSED_INVOICES_JSON = OUTPUT_DIR / "parsed_invoices.json"
BULK_UPLOAD_MAX_FILES = 500
BULK_PROCESSING_BATCH_SIZE = 50
BULK_PROCESSING_MAX_WORKERS = 16

SECRET_KEY = "replace-me-before-production"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
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

ROOT_URLCONF = "config.urls"

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
            "timeout": 20,
        },
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Denver"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Allow large direct-PDF bulk uploads while keeping large file bodies on disk
# instead of in memory as early as possible.
DATA_UPLOAD_MAX_NUMBER_FILES = BULK_UPLOAD_MAX_FILES
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 512 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── ML / Classifier configuration ────────────────────────────────────────────
# Edit these values to tune the classifier without touching service code.
# Restart the dev server after saving changes (make run).
#
# Suggested tuning workflow:
#   1. Upload a batch of invoices and review them.
#   2. If the top suggestion is frequently wrong, lower INVOICE_GL_PRIOR
#      so the embedding and KNN signals have more influence.
#   3. If the model overrides the invoice GL too aggressively, raise it.
#   4. Once you have 200+ approved items, lower KNN_MIN_SIMILARITY slightly
#      to cast a wider net over history.
#   5. Once you have 500+ approved items, lower TIER_AUTO_APPROVE_AGREE
#      to let more items auto-approve without review.

ML_CONFIG = {

    # ── Scoring weights ───────────────────────────────────────────────────────

    # Multiplied by the embedding cosine similarity (0–1).
    # This affects both the static GL-description match and the KNN vote.
    # Raise to trust embeddings more; lower to trust them less.
    "EMBEDDING_WEIGHT": 4.0,

    # Flat bonus added to whichever GL code the invoice header declares.
    # Think of it as "the person who placed this order had a reason for picking
    # this GL, but may have added a few items that don't belong."
    # Raise to make the invoice GL stickier; lower to let ML override more.
    "INVOICE_GL_PRIOR": 4.0,

    # ── KNN (history) parameters ──────────────────────────────────────────────

    # How many nearest approved neighbors to consider when voting.
    # Raising this smooths out noise; lowering it makes the vote sharper.
    "KNN_K": 5,

    # Approved items below this cosine similarity are ignored as noise.
    # Lower this (e.g. 0.35) after you have 500+ approved items to cast
    # a wider net. Keep it high early on to avoid noisy votes.
    "KNN_MIN_SIMILARITY": 0.45,

    # ── Tiered approval thresholds ────────────────────────────────────────────
    # These control the auto-approve pipeline (not yet active — reserved for
    # future use once you have enough approved history to trust them).
    #
    # TIER_AUTO_APPROVE_AGREE:
    #   Confidence at or above this → auto-approve when suggestion = invoice GL.
    #   Start conservative (0.90). Lower to 0.80 once you trust the model.
    "TIER_AUTO_APPROVE_AGREE": 0.90,

    # TIER_AUTO_APPROVE_OVERRIDE:
    #   Confidence at or above this → auto-approve when suggestion ≠ invoice GL.
    #   Keep this very high — the model should be near-certain before overriding
    #   the invoice GL without a human in the loop.
    "TIER_AUTO_APPROVE_OVERRIDE": 0.95,

    # TIER_QUICK_CONFIRM_AGREE:
    #   Confidence at or above this → quick-confirm lane when suggestion = invoice GL.
    "TIER_QUICK_CONFIRM_AGREE": 0.60,

    # TIER_QUICK_CONFIRM_OVERRIDE:
    #   Confidence at or above this → quick-confirm lane when suggestion ≠ invoice GL.
    "TIER_QUICK_CONFIRM_OVERRIDE": 0.85,

    # Below all quick-confirm thresholds → full review queue (always shown).
}
