from django.core.management.base import BaseCommand
from django.conf import settings

from apps.invoices.models import GLAccount, Invoice, InvoiceLineItem, PropertyReference


class Command(BaseCommand):
    help = "Delete invoice data, reference codes, or both."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Also delete GL accounts and property references.",
        )
        parser.add_argument(
            "--codes-only",
            action="store_true",
            help="Delete GL accounts and property references without deleting invoices.",
        )

    def handle(self, *args, **options):
        wipe_all = options["all"]
        codes_only = options["codes_only"]

        if wipe_all and codes_only:
            self.stderr.write("Choose either --all or --codes-only, not both.")
            return

        clear_invoices = not codes_only
        clear_codes = wipe_all or codes_only

        invoice_count = Invoice.objects.count() if clear_invoices else 0
        line_item_count = InvoiceLineItem.objects.count() if clear_invoices else 0
        gl_count = GLAccount.objects.count() if clear_codes else 0
        prop_count = PropertyReference.objects.count() if clear_codes else 0

        if invoice_count == 0 and line_item_count == 0 and gl_count == 0 and prop_count == 0:
            self.stdout.write("Nothing to clear.")
            return

        if clear_invoices:
            self.stdout.write(
                f"This will delete {invoice_count} invoice(s) and {line_item_count} line item(s)."
            )
        if clear_codes:
            self.stdout.write(
                f"This will delete {gl_count} GL account(s) and {prop_count} property reference(s)."
            )

        if not options["yes"]:
            confirm = input("Type 'yes' to continue: ").strip().lower()
            if confirm != "yes":
                self.stdout.write("Cancelled.")
                return

        if clear_invoices:
            InvoiceLineItem.objects.all().delete()
            Invoice.objects.all().delete()

        if clear_codes:
            GLAccount.objects.all().delete()
            PropertyReference.objects.all().delete()

        # Also wipe the JSON snapshot so it doesn't show stale data.
        json_path = settings.PARSED_INVOICES_JSON
        if clear_invoices and json_path.exists():
            json_path.unlink()
            self.stdout.write(f"Removed {json_path}")

        msg_parts = []
        if clear_invoices:
            msg_parts.append(f"Cleared {invoice_count} invoice(s) and {line_item_count} line item(s).")
        if clear_codes:
            msg_parts.append(f"Cleared {gl_count} GL account(s) and {prop_count} property reference(s).")
        msg = " ".join(msg_parts)
        self.stdout.write(self.style.SUCCESS(msg))
