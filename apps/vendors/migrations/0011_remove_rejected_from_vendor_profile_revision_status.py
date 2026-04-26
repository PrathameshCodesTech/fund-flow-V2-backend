from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0010_vendor_approved_profile_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vendorprofilerevision",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("submitted", "Submitted"),
                    ("finance_approved", "Finance Approved"),
                    ("finance_rejected", "Finance Rejected"),
                    ("reopened", "Reopened"),
                    ("applied", "Applied"),
                    ("cancelled", "Cancelled"),
                ],
                default="draft",
                max_length=30,
            ),
        ),
    ]
