from django.conf import settings
from django import forms

from .models import GLAccount, PropertyReference

_MAX_PDF_SIZE = getattr(settings, "BULK_UPLOAD_MAX_FILE_SIZE_MB", 50) * 1024 * 1024


class InvoiceUploadForm(forms.Form):
    invoice_pdf = forms.FileField(
        required=True,
        help_text="Upload a single Amazon invoice PDF.",
    )

    def clean_invoice_pdf(self):
        uploaded_file = self.cleaned_data["invoice_pdf"]
        if not uploaded_file.name.lower().endswith(".pdf"):
            raise forms.ValidationError("Please upload a PDF file.")
        return uploaded_file


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_file_clean = super().clean

        if not data:
            return []

        if not isinstance(data, (list, tuple)):
            data = [data]

        cleaned_files = []
        errors = []
        for uploaded_file in data:
            try:
                cleaned_files.append(single_file_clean(uploaded_file, initial))
            except forms.ValidationError as exc:
                errors.extend(exc.error_list)

        if errors:
            raise forms.ValidationError(errors)

        return cleaned_files


class BulkInvoiceUploadForm(forms.Form):
    invoice_pdfs = MultipleFileField(
        required=True,
        help_text=(
            f"Upload up to {settings.BULK_UPLOAD_MAX_FILES} Amazon invoice PDFs."
        ),
    )

    def clean_invoice_pdfs(self):
        uploaded_files = self.cleaned_data["invoice_pdfs"]
        if not uploaded_files:
            raise forms.ValidationError("Please upload at least one PDF file.")
        if len(uploaded_files) > settings.BULK_UPLOAD_MAX_FILES:
            raise forms.ValidationError(
                f"You can upload up to {settings.BULK_UPLOAD_MAX_FILES} PDFs at once."
            )

        errors = []
        for f in uploaded_files:
            if not f.name.lower().endswith(".pdf"):
                errors.append(f"{f.name}: only PDF files are supported.")
                continue

            if f.size and f.size > _MAX_PDF_SIZE:
                mb = _MAX_PDF_SIZE // (1024 * 1024)
                errors.append(f"{f.name}: file exceeds the {mb} MB limit ({f.size // (1024 * 1024)} MB).")
                continue

            # Magic byte check — catch files renamed to .pdf that aren't PDFs.
            try:
                header = f.read(4)
                f.seek(0)
                if header != b"%PDF":
                    errors.append(f"{f.name}: does not appear to be a valid PDF file.")
            except Exception:
                f.seek(0)

        if errors:
            raise forms.ValidationError(errors)

        return uploaded_files


class GLAccountForm(forms.ModelForm):
    class Meta:
        model = GLAccount
        fields = ["code", "description", "in_review_range"]
        widgets = {
            "code": forms.TextInput(attrs={"placeholder": "6328"}),
            "description": forms.TextInput(attrs={"placeholder": "OFFICE EQUIPMENT PURCHASES"}),
        }


class PropertyReferenceForm(forms.ModelForm):
    class Meta:
        model = PropertyReference
        fields = ["code", "website_id", "display_name"]
        widgets = {
            "code": forms.TextInput(attrs={"placeholder": "SSOH"}),
            "website_id": forms.TextInput(attrs={"placeholder": "12345"}),
            "display_name": forms.TextInput(attrs={"placeholder": "Sunset Station"}),
        }

    def clean_code(self):
        return (self.cleaned_data.get("code") or "").strip().upper()

    def clean_display_name(self):
        return (self.cleaned_data.get("display_name") or "").strip()
