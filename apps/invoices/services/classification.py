import math

from django.conf import settings

from ..models import GLAccount
from ..schemas import GLSuggestion, InvoiceLineItem as ParsedLineItem
from . import embedding_classifier

_cfg = settings.ML_CONFIG
_EMBEDDING_WEIGHT    = _cfg["EMBEDDING_WEIGHT"]
_INVOICE_GL_PRIOR    = _cfg["INVOICE_GL_PRIOR"]
_REVIEW_RANGE_WEIGHT = _cfg["REVIEW_RANGE_WEIGHT"]


class LineItemGLClassifierService:
    def suggest(self, line_item: ParsedLineItem, invoice_gl_code: str) -> list[GLSuggestion]:
        if line_item.item_type in ("discount", "shipping"):
            return self._non_product_suggestions(line_item, invoice_gl_code, "Non-product line defaulted to invoice-level GL.")

        # All GL accounts are candidates — in_review_range no longer gates eligibility.
        candidates = list(GLAccount.objects.order_by("code"))

        # Signal 1 (static): semantic similarity against GL account descriptions.
        gl_embedding_scores = embedding_classifier.score_description_against_gl(
            line_item.description, candidates
        )

        # Signal 2 (grows with use): KNN vote over all previously approved items.
        knn_votes = embedding_classifier.score_against_approved_history(
            line_item.description
        )

        scored: list[GLSuggestion] = []

        for account in candidates:
            score = 0.0
            reasons: list[str] = []

            # Strong prior: invoice-level GL code.
            if invoice_gl_code and account.code == invoice_gl_code:
                score += _INVOICE_GL_PRIOR
                reasons.append("Matches the GL code recorded on the invoice.")

            # Small-to-medium prior: frequently-used range codes get a gentle boost.
            # Does not override the invoice GL or strong embedding/KNN signals.
            if account.in_review_range:
                score += _REVIEW_RANGE_WEIGHT
                reasons.append("In the commonly-used GL range.")

            # Static embedding: semantic similarity between line item and GL description.
            gl_sim = gl_embedding_scores.get(account.code, 0.0)
            if gl_sim > 0.0:
                contribution = round(gl_sim * _EMBEDDING_WEIGHT, 2)
                score += contribution
                reasons.append(
                    f"Semantic match against GL description ({gl_sim:.2f} similarity)."
                )

            # KNN history: vote from similar previously-approved items.
            knn_vote = knn_votes.get(account.code, 0.0)
            if knn_vote > 0.0:
                knn_contribution = round(knn_vote * _EMBEDDING_WEIGHT, 2)
                score += knn_contribution
                reasons.append(
                    f"Similar to previously approved items (KNN vote {knn_vote:.2f})."
                )

            if score <= 0:
                continue

            scored.append(
                GLSuggestion(
                    gl_code=account.code,
                    gl_description=account.description,
                    score=round(score, 2),
                    confidence=0.0,
                    reasons=reasons,
                )
            )

        if not scored:
            return self._fallback_suggestions(invoice_gl_code)

        scored.sort(key=lambda item: item.score, reverse=True)
        top_score = scored[0].score
        second_score = scored[1].score if len(scored) > 1 else 0.0
        score_gap = max(0.0, top_score - second_score)
        top_candidates = scored[:3]
        temperature = 2.0
        softmax_weights = [
            math.exp((suggestion.score - top_score) / temperature)
            for suggestion in top_candidates
        ]
        softmax_total = sum(softmax_weights) or 1.0

        for index, suggestion in enumerate(top_candidates):
            score_strength = suggestion.score / (suggestion.score + 3.0)
            relative_strength = softmax_weights[index] / softmax_total
            confidence = 0.35 + (score_strength * 0.25) + (relative_strength * 0.25)
            if suggestion.gl_code == scored[0].gl_code:
                margin_strength = score_gap / (score_gap + 2.0) if score_gap else 0.0
                confidence += margin_strength * 0.20
            suggestion.confidence = round(min(0.95, confidence), 2)

        return top_candidates

    def _non_product_suggestions(
        self,
        line_item: ParsedLineItem,
        invoice_gl_code: str,
        reason: str,
    ) -> list[GLSuggestion]:
        account = GLAccount.objects.filter(code=invoice_gl_code).first() if invoice_gl_code else None
        if account:
            return [
                GLSuggestion(
                    gl_code=account.code,
                    gl_description=account.description,
                    score=1.0,
                    confidence=1.0,
                    reasons=[reason],
                )
            ]
        return []

    def _fallback_suggestions(self, invoice_gl_code: str) -> list[GLSuggestion]:
        if not invoice_gl_code:
            return []
        account = GLAccount.objects.filter(code=invoice_gl_code).first()
        if not account:
            return []
        return [
            GLSuggestion(
                gl_code=account.code,
                gl_description=account.description,
                score=0.5,
                confidence=0.35,
                reasons=["No stronger match found, so the invoice-level GL was kept as a fallback."],
            )
        ]
