"""
Vendor V1 overhaul:
- Creates VendorInvitation, VendorOnboardingSubmission, VendorAttachment,
  VendorFinanceActionToken, VendorFinanceDecision tables.
- Replaces the old Vendor schema (name/status/created_by) with the V1 schema
  (vendor_name/operational_status/marketing_status/sap_vendor_id etc.).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0001_initial"),
        ("core", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── 1. Create VendorInvitation ─────────────────────────────────────
        migrations.CreateModel(
            name="VendorInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vendor_email", models.EmailField(max_length=254)),
                ("vendor_name_hint", models.CharField(blank=True, max_length=255)),
                ("token", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(
                    choices=[
                        ("pending", "Pending"), ("opened", "Opened"), ("submitted", "Submitted"),
                        ("expired", "Expired"), ("cancelled", "Cancelled"),
                    ],
                    default="pending", max_length=20,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("org", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="vendor_invitations", to="core.organization",
                )),
                ("scope_node", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="vendor_invitations", to="core.scopenode",
                )),
                ("invited_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="sent_vendor_invitations", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "db_table": "vendor_invitations",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["token"], name="vendor_inv_token_idx"),
                    models.Index(fields=["org", "status"], name="vendor_inv_org_status_idx"),
                    models.Index(fields=["vendor_email"], name="vendor_inv_email_idx"),
                ],
            },
        ),

        # ── 2. Create VendorOnboardingSubmission ───────────────────────────
        migrations.CreateModel(
            name="VendorOnboardingSubmission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("submission_mode", models.CharField(
                    choices=[("manual", "Manual"), ("excel_upload", "Excel Upload")],
                    default="manual", max_length=20,
                )),
                ("status", models.CharField(
                    choices=[
                        ("draft", "Draft"), ("submitted", "Submitted"),
                        ("sent_to_finance", "Sent to Finance"),
                        ("finance_approved", "Finance Approved"),
                        ("finance_rejected", "Finance Rejected"),
                        ("reopened", "Reopened"),
                        ("marketing_pending", "Marketing Pending"),
                        ("marketing_approved", "Marketing Approved"),
                        ("activated", "Activated"), ("rejected", "Rejected"),
                    ],
                    default="draft", max_length=30,
                )),
                ("raw_form_data", models.JSONField(blank=True, default=dict)),
                ("normalized_vendor_name", models.CharField(blank=True, max_length=255)),
                ("normalized_vendor_type", models.CharField(blank=True, max_length=100)),
                ("normalized_email", models.EmailField(blank=True)),
                ("normalized_phone", models.CharField(blank=True, max_length=50)),
                ("normalized_gst_registered", models.BooleanField(blank=True, null=True)),
                ("normalized_gstin", models.CharField(blank=True, max_length=20)),
                ("normalized_pan", models.CharField(blank=True, max_length=20)),
                ("normalized_address_line1", models.CharField(blank=True, max_length=255)),
                ("normalized_address_line2", models.CharField(blank=True, max_length=255)),
                ("normalized_city", models.CharField(blank=True, max_length=100)),
                ("normalized_state", models.CharField(blank=True, max_length=100)),
                ("normalized_country", models.CharField(blank=True, max_length=100)),
                ("normalized_pincode", models.CharField(blank=True, max_length=20)),
                ("normalized_bank_name", models.CharField(blank=True, max_length=255)),
                ("normalized_account_number", models.CharField(blank=True, max_length=50)),
                ("normalized_ifsc", models.CharField(blank=True, max_length=20)),
                ("source_excel_file", models.CharField(blank=True, max_length=500)),
                ("exported_excel_file", models.CharField(blank=True, max_length=500)),
                ("finance_sent_at", models.DateTimeField(blank=True, null=True)),
                ("finance_vendor_code", models.CharField(blank=True, max_length=100)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("invitation", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="submissions", to="vendors.vendorinvitation",
                )),
            ],
            options={
                "db_table": "vendor_onboarding_submissions",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["invitation", "status"], name="vendor_sub_inv_status_idx"),
                    models.Index(fields=["status"], name="vendor_sub_status_idx"),
                    models.Index(fields=["normalized_email"], name="vendor_sub_email_idx"),
                ],
            },
        ),

        # ── 3. Create VendorAttachment ─────────────────────────────────────
        migrations.CreateModel(
            name="VendorAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_type", models.CharField(blank=True, max_length=100)),
                ("title", models.CharField(max_length=255)),
                ("file_name", models.CharField(max_length=500)),
                ("file_url", models.CharField(blank=True, max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("submission", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="attachments", to="vendors.vendoronboardingsubmission",
                )),
                ("uploaded_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="vendor_attachments", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "db_table": "vendor_attachments",
                "ordering": ["-created_at"],
            },
        ),

        # ── 4. Create VendorFinanceActionToken ─────────────────────────────
        migrations.CreateModel(
            name="VendorFinanceActionToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action_type", models.CharField(
                    choices=[("approve", "Approve"), ("reject", "Reject")],
                    max_length=10,
                )),
                ("token", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("submission", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="finance_tokens", to="vendors.vendoronboardingsubmission",
                )),
            ],
            options={
                "db_table": "vendor_finance_action_tokens",
                "indexes": [
                    models.Index(fields=["token"], name="vendor_fat_token_idx"),
                    models.Index(fields=["submission", "action_type"], name="vendor_fat_sub_action_idx"),
                ],
            },
        ),

        # ── 5. Create VendorFinanceDecision ────────────────────────────────
        migrations.CreateModel(
            name="VendorFinanceDecision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("decision", models.CharField(
                    choices=[("approved", "Approved"), ("rejected", "Rejected")],
                    max_length=10,
                )),
                ("sap_vendor_id", models.CharField(blank=True, max_length=100)),
                ("note", models.TextField(blank=True)),
                ("acted_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("submission", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="finance_decisions", to="vendors.vendoronboardingsubmission",
                )),
                ("acted_via_token", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="decisions", to="vendors.vendorfinanceactiontoken",
                )),
            ],
            options={
                "db_table": "vendor_finance_decisions",
                "ordering": ["-acted_at"],
            },
        ),

        # ── 6. Modify Vendor table ─────────────────────────────────────────
        # Remove old index first
        migrations.RemoveIndex(
            model_name="vendor",
            name="vendors_scope_n_56ec0a_idx",
        ),
        # Remove old fields
        migrations.RemoveField(model_name="vendor", name="name"),
        migrations.RemoveField(model_name="vendor", name="status"),
        migrations.RemoveField(model_name="vendor", name="created_by"),
        # Add new fields
        migrations.AddField(
            model_name="vendor",
            name="org",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="vendors", to="core.organization",
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="onboarding_submission",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="vendor", to="vendors.vendoronboardingsubmission",
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="vendor_name",
            field=models.CharField(default="", max_length=255),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="vendor",
            name="sap_vendor_id",
            field=models.CharField(default="", max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="vendor",
            name="po_mandate_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="vendor",
            name="marketing_status",
            field=models.CharField(
                choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")],
                default="pending", max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="operational_status",
            field=models.CharField(
                choices=[
                    ("inactive", "Inactive"),
                    ("waiting_marketing_approval", "Waiting Marketing Approval"),
                    ("active", "Active"),
                    ("suspended", "Suspended"),
                ],
                default="inactive", max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="approved_by_marketing",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="marketing_approved_vendors", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Update model options (ordering)
        migrations.AlterModelOptions(
            name="vendor",
            options={"ordering": ["vendor_name"]},
        ),
        # Add new indexes
        migrations.AddIndex(
            model_name="vendor",
            index=models.Index(fields=["org", "operational_status"], name="vendors_org_opstatus_idx"),
        ),
        migrations.AddIndex(
            model_name="vendor",
            index=models.Index(fields=["scope_node", "operational_status"], name="vendors_scope_opstatus_idx"),
        ),
        migrations.AddIndex(
            model_name="vendor",
            index=models.Index(fields=["sap_vendor_id"], name="vendors_sap_id_idx"),
        ),
    ]
