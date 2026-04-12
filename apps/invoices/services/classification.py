import re

from ..models import GLAccount
from ..schemas import GLSuggestion, InvoiceLineItem as ParsedLineItem
from . import embedding_classifier

# Weight applied to the embedding cosine similarity score.
# Cosine similarity is in [0, 1]; multiplying by this weight puts it on a
# comparable scale to the keyword and token-overlap signals.
_EMBEDDING_WEIGHT = 3.0

# Bonus added to the invoice-level GL code when scoring competing accounts.
# At 4.0 this code wins by default; overriding it requires strong evidence
# such as multiple keyword hits, a high-similarity embedding, or a clear
# KNN vote from prior approved items.  Raise this value to make the invoice
# GL "stickier"; lower it to let the classifier override more easily.
_INVOICE_GL_PRIOR = 4.0


STOP_WORDS = {
    "and",
    "for",
    "the",
    "with",
    "set",
    "kit",
    "inch",
    "pack",
    "each",
    "piece",
    "pieces",
    "heavy",
    "duty",
    "new",
}


GL_KEYWORD_HINTS = {
    "6328": ["monitor", "headset", "office chair", "shredder", "equipment"],
    "6329": ["bluetooth", "usb", "adapter", "keyboard", "mouse", "router", "tech", "tracker"],
    "6332": ["paper", "copy paper", "pencil", "pen", "folder", "notebook", "stapler", "office supplies", "labels"],
    "6396": ["snack", "cookies", "cakes", "brownies", "honey buns", "swiss rolls", "food", "beverage", "water", "coffee"],
    "6702": ["dryer", "washer", "appliance", "microwave", "refrigerator", "vacuum", "small appliance", "seal", "bearing"],
    "6710": ["electrical", "led", "switch", "outlet", "battery", "charger", "extension", "light", "fixture"],
    "6714": ["tool", "drill", "ladder", "equipment purchase", "maintenance tool"],
    "6718": ["fire", "safety", "extinguisher", "detector", "alarm"],
    "6726": ["lock", "key", "deadbolt", "keypad"],
    "6728": ["paint", "drywall", "primer", "roller", "brush", "caulk"],
    "6730": ["plumbing", "toilet", "faucet", "drain", "pipe", "shower", "valve"],
    "6734": ["pool", "tetherball", "recreation", "outdoor", "playground", "basketball", "soccer"],
    "6512": ["furniture", "desk", "table", "lamp", "couch", "chair"],
}


class LineItemGLClassifierService:
    def suggest(self, line_item: ParsedLineItem, invoice_gl_code: str) -> list[GLSuggestion]:
        if line_item.item_type in ("discount", "shipping"):
            return self._non_product_suggestions(line_item, invoice_gl_code, "Non-product line defaulted to invoice-level GL.")

        candidates = list(GLAccount.objects.filter(in_review_range=True).order_by("code"))
        scored: list[GLSuggestion] = []
        description_tokens = self._tokenize(line_item.description)

        # Signal 1 (static): semantic similarity against GL account descriptions.
        gl_embedding_scores = embedding_classifier.score_description_against_gl(
            line_item.description, candidates
        )

        # Signal 2 (grows with use): KNN vote over all previously approved items.
        # Each approval adds a data point — accuracy increases over time.
        knn_votes = embedding_classifier.score_against_approved_history(
            line_item.description
        )

        for account in candidates:
            score = 0.0
            reasons: list[str] = []

            # Token overlap against GL description (fast, exact-token signal).
            chart_overlap = sorted(description_tokens & self._tokenize(account.description))
            if chart_overlap:
                score += len(chart_overlap) * 1.2
                reasons.append(f"GL description overlap: {', '.join(chart_overlap[:3])}.")

            # Hard-coded keyword hints (fast fallback for common items).
            keyword_hits = self._keyword_hits(line_item.description, account.code)
            if keyword_hits:
                score += len(keyword_hits) * 2.5
                reasons.append(f"Keyword match: {', '.join(keyword_hits[:3])}.")

            # Strong prior: invoice-level GL code.
            # This is the default choice — other signals must overcome it.
            if invoice_gl_code and account.code == invoice_gl_code:
                score += _INVOICE_GL_PRIOR
                reasons.append("Matches the GL code recorded on the invoice.")

            # Static embedding: GL description vs. line item description.
            gl_sim = gl_embedding_scores.get(account.code, 0.0)
            if gl_sim > 0.0:
                contribution = round(gl_sim * _EMBEDDING_WEIGHT, 2)
                score += contribution
                reasons.append(
                    f"Semantic match against GL description ({gl_sim:.2f} similarity)."
                )

            # KNN history: vote from similar previously-approved items.
            # This signal strengthens with every human approval.
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

        for suggestion in scored[:3]:
            confidence = min(0.95, 0.35 + (suggestion.score / max(top_score, 1.0)) * 0.35)
            if suggestion.gl_code == scored[0].gl_code:
                confidence += min(0.2, score_gap * 0.05)
            suggestion.confidence = round(confidence, 2)

        return scored[:3]

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

    def _keyword_hits(self, description: str, gl_code: str) -> list[str]:
        lowered = description.lower()
        return [keyword for keyword in GL_KEYWORD_HINTS.get(gl_code, []) if keyword in lowered]

    def _tokenize(self, value: str) -> set[str]:
        tokens = set()
        for token in re.findall(r"[a-z0-9]+", value.lower()):
            if len(token) < 3 or token in STOP_WORDS:
                continue
            tokens.add(token)
        return tokens

