from typing import BinaryIO


class PdfReaderService:
    """
    Extract raw text from PDF files.

    The implementation is intentionally narrow: it only concerns itself with
    text extraction and leaves invoice-specific interpretation to later stages.
    """

    def extract_text(self, file_obj: BinaryIO) -> str:
        try:
            import pdfplumber
        except ImportError as exc:
            raise RuntimeError(
                "pdfplumber is not installed yet. Install requirements before extracting text."
            ) from exc

        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

        with pdfplumber.open(file_obj) as pdf:
            page_text = [(page.extract_text() or "").strip() for page in pdf.pages]

        extracted_text = "\n\n".join(text for text in page_text if text)

        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

        return extracted_text
