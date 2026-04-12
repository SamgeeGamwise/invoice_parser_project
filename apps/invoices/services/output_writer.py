import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from django.conf import settings

from .orchestrator import BulkProcessingResult


def _json_default(obj):
    """Serialize types that the standard encoder can't handle."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class InvoiceOutputWriterService:
    """
    Persist bulk parse results to a single JSON file.

    The file is always overwritten so the output represents the most recent
    bulk run. The output path is controlled by settings.PARSED_INVOICES_JSON.
    """

    def __init__(self, output_path: Path | None = None) -> None:
        self.output_path: Path = output_path or settings.PARSED_INVOICES_JSON

    def write(self, result: BulkProcessingResult) -> Path:
        """
        Write result to disk and return the resolved output path.

        Creates the parent directory if it does not yet exist.
        """
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "total_files": result.success_count + result.error_count,
            "success_count": result.success_count,
            "error_count": result.error_count,
            "invoices": [inv.to_dict() for inv in result.invoices],
            "errors": result.errors,
        }

        with self.output_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=_json_default)

        return self.output_path
