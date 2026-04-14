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

        # Validate magic bytes — reject non-PDF files that slipped past extension checks.
        header = file_obj.read(5)
        if header[:4] != b"%PDF":
            raise ValueError(
                "File does not appear to be a valid PDF (invalid header). "
                "Only standard PDF files are supported."
            )
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

        try:
            with pdfplumber.open(file_obj) as pdf:
                if not pdf.pages:
                    raise ValueError("PDF has no pages.")

                page_text = []
                for i, page in enumerate(pdf.pages):
                    try:
                        text = (page.extract_text() or "").strip()
                    except Exception:
                        text = ""  # skip unreadable pages rather than aborting
                    page_text.append(text)

        except ValueError:
            raise
        except Exception as exc:
            # pdfplumber raises various exceptions for corrupt/encrypted/truncated PDFs.
            msg = str(exc).lower()
            if "encrypt" in msg or "password" in msg:
                raise ValueError(
                    "PDF is password-protected and cannot be processed."
                ) from exc
            raise ValueError(
                f"Could not open PDF file: {type(exc).__name__}. "
                "The file may be corrupt, truncated, or in an unsupported format."
            ) from exc

        extracted_text = "\n\n".join(text for text in page_text if text)

        if not extracted_text:
            raise ValueError(
                "PDF contains no extractable text. "
                "It may be blank, image-only (scanned), or encrypted."
            )

        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

        return extracted_text
