from django.core.management.base import BaseCommand
from django.conf import settings

from apps.invoices.models import Invoice, InvoiceLineItem


class Command(BaseCommand):
    help = "Delete all uploaded invoice data (invoices and line items). Reference data and ML model are untouched."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt.",
        )

    def handle(self, *args, **options):
        invoice_count = Invoice.objects.count()
        line_item_count = InvoiceLineItem.objects.count()

        if invoice_count == 0 and line_item_count == 0:
            self.stdout.write("Nothing to clear.")
            return

        self.stdout.write(
            f"This will delete {invoice_count} invoice(s) and {line_item_count} line item(s)."
        )
        self.stdout.write("GL accounts, property references, and the ML model are NOT affected.")

        if not options["yes"]:
            confirm = input("Type 'yes' to continue: ").strip().lower()
            if confirm != "yes":
                self.stdout.write("Cancelled.")
                return

        InvoiceLineItem.objects.all().delete()
        Invoice.objects.all().delete()

        # Also wipe the JSON snapshot so it doesn't show stale data.
        json_path = settings.PARSED_INVOICES_JSON
        if json_path.exists():
            json_path.unlink()
            self.stdout.write(f"Removed {json_path}")

        self.stdout.write(self.style.SUCCESS(
            f"Cleared {invoice_count} invoice(s) and {line_item_count} line item(s)."
        ))
