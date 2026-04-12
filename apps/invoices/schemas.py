from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass
class SourceFileInfo:
    name: str = ""
    size_bytes: int | None = None
    content_type: str = ""


@dataclass
class GLSuggestion:
    gl_code: str
    gl_description: str
    score: float
    confidence: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InvoiceLineItem:
    line_number: int = 0
    item_type: str = "product"
    description: str = ""
    normalized_description: str = ""
    quantity: int | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    asin: str = ""
    vendor: str = ""
    order_number: str = ""
    invoice_gl_code_hint: str = ""
    suggested_gl_code: str = ""
    suggested_gl_description: str = ""
    suggested_confidence: float = 0.0
    suggestion_reason: str = ""
    suggestion_candidates: list[GLSuggestion] = field(default_factory=list)
    approved_gl_code: str = ""
    approved_gl_description: str = ""
    approval_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["suggestion_candidates"] = [candidate.to_dict() for candidate in self.suggestion_candidates]
        return payload


@dataclass
class ParsedInvoice:
    source_file: SourceFileInfo = field(default_factory=SourceFileInfo)
    invoice_number: str = ""
    invoice_date: date | None = None
    purchase_date: date | None = None
    purchaser: str = ""
    po_number: str = ""
    invoice_gl_code: str = ""
    invoice_gl_description: str = ""
    property_code_raw: str = ""
    property_code_normalized: str = ""
    property_code_validated: bool = False
    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    grand_total: Decimal | None = None
    line_items: list[InvoiceLineItem] = field(default_factory=list)
    raw_text: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["line_items"] = [line_item.to_dict() for line_item in self.line_items]
        return payload
