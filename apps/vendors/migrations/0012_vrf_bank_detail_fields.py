from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0011_remove_rejected_from_vendor_profile_revision_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendoronboardingsubmission",
            name="normalized_beneficiary_account_number",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="vendoronboardingsubmission",
            name="normalized_bank_address",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="vendoronboardingsubmission",
            name="normalized_bank_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="vendoronboardingsubmission",
            name="normalized_bank_account_number",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="vendor",
            name="beneficiary_account_number",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="vendor",
            name="bank_address",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="vendor",
            name="bank_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="vendor",
            name="bank_account_number",
            field=models.CharField(blank=True, max_length=50),
        ),
    ]
