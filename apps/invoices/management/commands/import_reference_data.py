from django.core.management.base import BaseCommand

from apps.invoices.models import GLAccount, PropertyReference
from apps.invoices.services.reference_data import ReferenceDataSyncService


class Command(BaseCommand):
    help = "Import GL accounts and property references from the Excel files into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-read the spreadsheet files even if rows already exist.",
        )

    def handle(self, *args, **options):
        service = ReferenceDataSyncService()
        before_gl = GLAccount.objects.count()
        before_prop = PropertyReference.objects.count()

        service.sync_all(force=options["force"])

        after_gl = GLAccount.objects.count()
        after_prop = PropertyReference.objects.count()

        self.stdout.write(
            self.style.SUCCESS(
                f"Reference data import complete. "
                f"GL accounts: {before_gl} -> {after_gl}. "
                f"Property references: {before_prop} -> {after_prop}."
            )
        )
