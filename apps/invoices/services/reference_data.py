from dataclasses import dataclass
from pathlib import Path
import threading

from django.conf import settings

from ..models import GLAccount, PropertyReference
from .spreadsheet_reader import SpreadsheetReaderService


@dataclass
class PropertyMatch:
    normalized_code: str
    is_valid: bool
    property_reference: PropertyReference | None


class ReferenceDataSyncService:
    _sync_lock = threading.Lock()

    def __init__(self, spreadsheet_reader: SpreadsheetReaderService | None = None) -> None:
        self.spreadsheet_reader = spreadsheet_reader or SpreadsheetReaderService()

    def sync_all(self, force: bool = False) -> None:
        with self._sync_lock:
            self.sync_gl_accounts(force=force)
            self.sync_property_references(force=force)

    def ensure_loaded(self) -> None:
        if not GLAccount.objects.exists() or not PropertyReference.objects.exists():
            raise RuntimeError(
                "Reference data has not been imported into the database yet. "
                "Import GL accounts and property references before processing invoices."
            )

    def sync_gl_accounts(self, force: bool = False) -> None:
        if not force and GLAccount.objects.exists():
            return
        rows = self.spreadsheet_reader.read_rows(settings.REFERENCE_DATA_DIR / "GL List.xlsx")
        if not rows:
            return

        for row in rows[1:]:
            if len(row) < 2:
                continue
            code, description = row[0], row[1]
            if not code or not description:
                continue
            GLAccount.objects.update_or_create(
                code=code.strip(),
                defaults={
                    "description": description.strip(),
                    "in_review_range": self._in_review_range(code.strip()),
                },
            )

    def sync_property_references(self, force: bool = False) -> None:
        if not force and PropertyReference.objects.exists():
            return
        rows = self.spreadsheet_reader.read_rows(settings.REFERENCE_DATA_DIR / "Property List.xlsx")
        if not rows:
            return

        for row in rows[1:]:
            if len(row) < 2:
                continue
            website_id, yardi_code = row[0], row[1]
            if not yardi_code:
                continue
            extras = [value.strip() for value in row[2:] if value and value.strip()]
            display_name = extras[0] if extras else ""
            normalized_code = self.normalize_property_code(yardi_code)
            PropertyReference.objects.update_or_create(
                yardi_code=yardi_code.strip(),
                defaults={
                    "website_id": website_id.strip(),
                    "normalized_code": normalized_code,
                    "display_name": display_name,
                },
            )

    def match_property_code(self, property_code: str) -> PropertyMatch:
        normalized_code = self.normalize_property_code(property_code)
        property_reference = PropertyReference.objects.filter(normalized_code=normalized_code).first()
        return PropertyMatch(
            normalized_code=normalized_code,
            is_valid=property_reference is not None,
            property_reference=property_reference,
        )

    def get_gl_description(self, gl_code: str) -> str:
        account = GLAccount.objects.filter(code=gl_code).first()
        return account.description if account else ""

    def normalize_property_code(self, value: str) -> str:
        return value.strip().upper()

    def _in_review_range(self, code: str) -> bool:
        return code.isdigit() and 6000 <= int(code) <= 7070
