"""
Embedding-based GL code scorer.

Two scoring functions are provided:

1. score_description_against_gl()
   Compares a line item description against each GL account's description text.
   Uses the static GL chart of accounts. Does NOT improve with use — the model
   weights and the GL descriptions are both fixed.

2. score_against_approved_history()
   Compares a line item description against every previously approved line item
   in the database using K-Nearest Neighbors over sentence embeddings.
   THIS IS THE FUNCTION THAT GROWS MORE ACCURATE WITH USE.
   Every new human approval adds a data point. Over time the KNN vote becomes
   the dominant and most reliable signal.

Model: sentence-transformers/all-MiniLM-L6-v2
  - ~80 MB download, CPU-friendly, no GPU required
  - Produces 384-dimensional sentence embeddings
  - Cached locally by sentence-transformers in ~/.cache/huggingface/
  - Weights are frozen — the model itself does not change

Caching strategy:
  - The model is a process-level singleton (loaded once, reused forever).
  - GL embeddings are cached by the set of GL codes (stable, rarely changes).
  - Approved history embeddings are cached by approved item count. When a
    reviewer approves a new item the count changes, the cache is rebuilt on
    the next classification request, and the new approval is immediately
    incorporated into future suggestions.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import GLAccount

logger = logging.getLogger(__name__)


# ── Model singleton ──────────────────────────────────────────────────────────

_model = None
_model_lock = threading.Lock()
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _get_model():
    """Return the shared model instance, loading it on first call."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading sentence-transformer model '%s'…", _MODEL_NAME)
            _model = SentenceTransformer(_MODEL_NAME)
            logger.info("Model loaded.")
        except ImportError:
            logger.warning(
                "sentence-transformers is not installed. "
                "Embedding scores will be 0.0. "
                "Run: pip install sentence-transformers"
            )
            _model = None
    return _model


# ── GL description embedding cache ──────────────────────────────────────────
# Keyed by the frozenset of GL codes present. Rarely needs rebuilding.

_gl_cache: dict[frozenset, dict[str, object]] = {}
_gl_cache_lock = threading.Lock()


def _get_gl_embeddings(gl_accounts: list["GLAccount"]) -> dict[str, object]:
    cache_key = frozenset(a.code for a in gl_accounts)
    with _gl_cache_lock:
        if cache_key in _gl_cache:
            return _gl_cache[cache_key]

    model = _get_model()
    if model is None:
        return {}

    texts = [f"{a.code} {a.description}" for a in gl_accounts]
    vectors = model.encode(texts, convert_to_numpy=True)
    result = {account.code: vectors[i] for i, account in enumerate(gl_accounts)}

    with _gl_cache_lock:
        _gl_cache[cache_key] = result
    return result


# ── Approved history embedding cache ────────────────────────────────────────
# Rebuilt whenever the number of approved line items in the DB changes.
# Stores a parallel list of (gl_code, embedding) for KNN lookup.

_history_cache_count: int = -1
_history_cache_vectors = None   # numpy array shape (N, 384)
_history_cache_gl_codes: list[str] = []
_history_lock = threading.Lock()


def _get_approved_history_embeddings() -> tuple[object, list[str]]:
    """
    Return (matrix_of_embeddings, list_of_gl_codes) for all approved line items.

    The matrix row order matches the gl_codes list. Both are empty if there are
    no approvals yet or if the model is unavailable.
    """
    import numpy as np
    from ..models import InvoiceLineItem

    model = _get_model()
    if model is None:
        return np.array([]), []

    approved = list(
        InvoiceLineItem.objects
        .filter(approved_gl__isnull=False, item_type=InvoiceLineItem.ItemType.PRODUCT)
        .select_related("approved_gl")
        .values_list("description", "approved_gl__code")
    )

    current_count = len(approved)

    global _history_cache_count, _history_cache_vectors, _history_cache_gl_codes

    with _history_lock:
        if current_count == _history_cache_count and _history_cache_vectors is not None:
            return _history_cache_vectors, _history_cache_gl_codes

        if current_count == 0:
            _history_cache_count = 0
            _history_cache_vectors = np.array([])
            _history_cache_gl_codes = []
            return _history_cache_vectors, _history_cache_gl_codes

        descriptions = [row[0] for row in approved]
        gl_codes = [row[1] for row in approved]

        logger.debug(
            "Rebuilding approved history embeddings for %d items.", current_count
        )
        vectors = model.encode(descriptions, convert_to_numpy=True, batch_size=64)

        _history_cache_count = current_count
        _history_cache_vectors = vectors
        _history_cache_gl_codes = gl_codes

        return _history_cache_vectors, _history_cache_gl_codes


# ── Public interface ─────────────────────────────────────────────────────────

def score_description_against_gl(
    description: str,
    gl_accounts: list["GLAccount"],
) -> dict[str, float]:
    """
    Semantic similarity between a description and each GL account description.

    Returns scores in [0, 1]. Does not improve with use — both the model
    weights and the GL descriptions are static.
    """
    import numpy as np

    model = _get_model()
    if model is None or not gl_accounts:
        return {}

    gl_embeddings = _get_gl_embeddings(gl_accounts)
    if not gl_embeddings:
        return {}

    item_vec = model.encode(description, convert_to_numpy=True)
    item_norm = np.linalg.norm(item_vec)
    if item_norm == 0:
        return {}

    scores: dict[str, float] = {}
    for account in gl_accounts:
        gl_vec = gl_embeddings.get(account.code)
        if gl_vec is None:
            continue
        gl_norm = np.linalg.norm(gl_vec)
        if gl_norm == 0:
            scores[account.code] = 0.0
            continue
        cosine = float(np.dot(item_vec, gl_vec) / (item_norm * gl_norm))
        scores[account.code] = max(0.0, cosine)

    return scores


def score_against_approved_history(
    description: str,
    k: int = 5,
    min_similarity: float = 0.45,
) -> dict[str, float]:
    """
    KNN vote over previously approved line items using embedding similarity.

    THIS IS THE FUNCTION THAT GROWS MORE ACCURATE WITH USE.

    For each of the K nearest approved items (by cosine similarity), the
    approved GL code receives a vote weighted by the similarity score. The
    returned dict maps GL code → total weighted vote (higher = stronger signal).

    Parameters
    ----------
    description     : text of the line item being classified
    k               : number of nearest neighbors to consider
    min_similarity  : neighbors below this threshold are ignored (noise filter)

    Returns an empty dict if there are no approved items or the model is
    unavailable. The caller should treat a missing key as a score of 0.
    """
    import numpy as np

    model = _get_model()
    if model is None:
        return {}

    history_vectors, history_gl_codes = _get_approved_history_embeddings()
    if len(history_vectors) == 0:
        return {}

    item_vec = model.encode(description, convert_to_numpy=True)
    item_norm = np.linalg.norm(item_vec)
    if item_norm == 0:
        return {}

    # Compute cosine similarity against every approved item at once.
    history_norms = np.linalg.norm(history_vectors, axis=1)
    safe_norms = np.where(history_norms == 0, 1e-10, history_norms)
    similarities = np.dot(history_vectors, item_vec) / (safe_norms * item_norm)
    similarities = np.clip(similarities, 0.0, 1.0)

    # Take the top K above the minimum threshold.
    top_k_indices = np.argsort(similarities)[-k:][::-1]

    votes: dict[str, float] = {}
    for idx in top_k_indices:
        sim = float(similarities[idx])
        if sim < min_similarity:
            break  # sorted descending, so all remaining are below threshold too
        gl_code = history_gl_codes[idx]
        votes[gl_code] = votes.get(gl_code, 0.0) + sim

    return votes


def clear_gl_cache() -> None:
    """Discard cached GL description embeddings (call after reference data sync)."""
    with _gl_cache_lock:
        _gl_cache.clear()


def clear_history_cache() -> None:
    """Force the approved history cache to rebuild on next classification."""
    global _history_cache_count
    with _history_lock:
        _history_cache_count = -1
