from django.conf import settings
from django import forms

from .models import GLAccount, PropertyReference


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

        invalid_files = [
            uploaded_file.name
            for uploaded_file in uploaded_files
            if not uploaded_file.name.lower().endswith(".pdf")
        ]
        if invalid_files:
            invalid_names = ", ".join(invalid_files)
            raise forms.ValidationError(
                f"Only PDF files are supported. Invalid files: {invalid_names}"
            )

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
        fields = ["yardi_code", "normalized_code", "website_id", "display_name"]
        widgets = {
            "yardi_code": forms.TextInput(attrs={"placeholder": "SSOH"}),
            "normalized_code": forms.TextInput(attrs={"placeholder": "SSOH"}),
            "website_id": forms.TextInput(attrs={"placeholder": "12345"}),
            "display_name": forms.TextInput(attrs={"placeholder": "Sunset Station"}),
        }

    def clean_normalized_code(self):
        return (self.cleaned_data.get("normalized_code") or "").strip().upper()

    def clean_yardi_code(self):
        return (self.cleaned_data.get("yardi_code") or "").strip()

    def clean_display_name(self):
        return (self.cleaned_data.get("display_name") or "").strip()
