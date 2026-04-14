from django.core.management.base import BaseCommand
from django.conf import settings

from apps.invoices.models import GLAccount, Invoice, InvoiceLineItem, PropertyReference


class Command(BaseCommand):
    help = "Delete invoice data. Use --all to also wipe GL codes and property references."

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

    def handle(self, *args, **options):
        wipe_all = options["all"]

        invoice_count = Invoice.objects.count()
        line_item_count = InvoiceLineItem.objects.count()
        gl_count = GLAccount.objects.count() if wipe_all else 0
        prop_count = PropertyReference.objects.count() if wipe_all else 0

        if invoice_count == 0 and line_item_count == 0 and gl_count == 0 and prop_count == 0:
            self.stdout.write("Nothing to clear.")
            return

        self.stdout.write(
            f"This will delete {invoice_count} invoice(s) and {line_item_count} line item(s)."
        )
        if wipe_all:
            self.stdout.write(
                f"Also deleting {gl_count} GL account(s) and {prop_count} property reference(s)."
            )

        if not options["yes"]:
            confirm = input("Type 'yes' to continue: ").strip().lower()
            if confirm != "yes":
                self.stdout.write("Cancelled.")
                return

        InvoiceLineItem.objects.all().delete()
        Invoice.objects.all().delete()

        if wipe_all:
            GLAccount.objects.all().delete()
            PropertyReference.objects.all().delete()

        # Also wipe the JSON snapshot so it doesn't show stale data.
        json_path = settings.PARSED_INVOICES_JSON
        if json_path.exists():
            json_path.unlink()
            self.stdout.write(f"Removed {json_path}")

        msg = f"Cleared {invoice_count} invoice(s) and {line_item_count} line item(s)."
        if wipe_all:
            msg += f" Also cleared {gl_count} GL account(s) and {prop_count} property reference(s)."
        self.stdout.write(self.style.SUCCESS(msg))
