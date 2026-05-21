from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0011_vendorinvoicesubmission_correction_note_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendorinvoicesubmission",
            name="original_normalized_data",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
