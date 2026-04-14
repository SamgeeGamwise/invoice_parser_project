"""
Migration: consolidate PropertyReference.yardi_code + normalized_code into a single `code` field.

Steps:
  1. Add `code` as nullable (so existing rows don't break).
  2. Populate `code` from `normalized_code` (already uppercase).
  3. Make `code` non-nullable + unique.
  4. Remove `yardi_code` and `normalized_code`.
  5. Make `website_id` non-nullable (no blank).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0005_remove_approval_notes"),
    ]

    operations = [
        # Step 1: add nullable code field
        migrations.AddField(
            model_name="propertyreference",
            name="code",
            field=models.CharField(max_length=20, null=True),
        ),

        # Step 2: populate from normalized_code
        migrations.RunSQL(
            sql='UPDATE invoices_propertyreference SET code = normalized_code',
            reverse_sql='UPDATE invoices_propertyreference SET normalized_code = code',
        ),

        # Step 3: make code non-nullable and unique
        migrations.AlterField(
            model_name="propertyreference",
            name="code",
            field=models.CharField(max_length=20, unique=True),
        ),

        # Step 4: remove the two old fields
        migrations.RemoveField(
            model_name="propertyreference",
            name="yardi_code",
        ),
        migrations.RemoveField(
            model_name="propertyreference",
            name="normalized_code",
        ),

        # Step 5: make website_id non-nullable
        migrations.AlterField(
            model_name="propertyreference",
            name="website_id",
            field=models.CharField(max_length=20),
        ),

        # Step 6: update ordering in Meta (handled automatically via AlterModelOptions)
        migrations.AlterModelOptions(
            name="propertyreference",
            options={"ordering": ["code"]},
        ),
    ]
