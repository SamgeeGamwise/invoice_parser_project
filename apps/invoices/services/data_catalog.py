from pathlib import Path

from django.conf import settings


class ProjectDataCatalogService:
    """
    Provide a small, read-only view of local project data directories.

    This keeps path knowledge out of views and creates a natural home for
    future file-discovery logic, fixture selection, and reference-data loading.
    """

    def list_sample_invoices(self) -> list[str]:
        return self._list_filenames(settings.SAMPLE_INVOICES_DIR, "*.pdf")

    def list_reference_files(self) -> list[str]:
        return self._list_filenames(settings.REFERENCE_DATA_DIR, "*")

    def _list_filenames(self, directory: Path, pattern: str) -> list[str]:
        if not directory.exists():
            return []
        return sorted(path.name for path in directory.glob(pattern) if path.is_file())
