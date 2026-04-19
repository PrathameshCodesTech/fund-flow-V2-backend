# Generated manually — Vendor Invoice Submission intake layer

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0005_vendor_activation_and_user_binding"),
        ("invoices", "0004_invoice_vendor_po_number"),
        ("core", "0001_initial"),
    ]

    operations = [
        # ── VendorInvoiceSubmission ──────────────────────────────────────────────
        migrations.CreateModel(
            name="VendorInvoiceSubmission",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "vendor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_submissions",
                        to="vendors.vendor",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.SET_NULL,
                        null=True,
                        related_name="vendor_invoice_submissions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "scope_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="vendor_invoice_submissions",
                        to="core.ScopeNode",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("uploaded", "Uploaded"),
                            ("extracting", "Extracting"),
                            ("needs_correction", "Needs Correction"),
                            ("ready", "Ready"),
                            ("submitted", "Submitted"),
                            ("rejected", "Rejected"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="uploaded",
                        max_length=30,
                    ),
                ),
                (
                    "source_file",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to="vendor_invoice_submissions/source_files/",
                    ),
                ),
                ("source_file_name", models.CharField(blank=True, max_length=500)),
                (
                    "source_file_type",
                    models.CharField(
                        choices=[("pdf", "PDF"), ("xlsx", "Excel"), ("xls", "Excel")],
                        max_length=10,
                    ),
                ),
                ("source_file_hash", models.CharField(blank=True, max_length=64)),
                ("raw_extracted_data", models.JSONField(blank=True, default=dict)),
                ("normalized_data", models.JSONField(blank=True, default=dict)),
                ("validation_errors", models.JSONField(blank=True, default=list)),
                (
                    "confidence_score",
                    models.DecimalField(
                        blank=True,
                        decimal_places=3,
                        max_digits=5,
                        null=True,
                    ),
                ),
                (
                    "final_invoice",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="submission",
                        to="invoices.invoice",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "vendor_invoice_submissions",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["vendor", "status"], name="vis_vendor_status_idx"
                    ),
                    models.Index(
                        fields=["vendor", "source_file_hash"],
                        name="vis_vendor_hash_idx",
                    ),
                    models.Index(
                        fields=["submitted_by"], name="vis_submitted_by_idx"
                    ),
                ],
            },
        ),

        # ── InvoiceDocument ───────────────────────────────────────────────────────
        migrations.CreateModel(
            name="InvoiceDocument",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="documents",
                        to="invoices.invoice",
                    ),
                ),
                (
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="documents",
                        to="invoices.VendorInvoiceSubmission",
                    ),
                ),
                (
                    "file",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to="vendor_invoice_documents/files/",
                    ),
                ),
                ("file_name", models.CharField(blank=True, max_length=500)),
                (
                    "file_type",
                    models.CharField(
                        choices=[
                            ("pdf", "PDF"),
                            ("xlsx", "Excel"),
                            ("xls", "Excel"),
                            ("png", "PNG"),
                            ("jpg", "JPG"),
                            ("jpeg", "JPEG"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "document_type",
                    models.CharField(
                        choices=[
                            ("invoice_pdf", "Invoice PDF"),
                            ("invoice_excel", "Invoice Excel"),
                            ("po_copy", "PO Copy"),
                            ("delivery_challan", "Delivery Challan"),
                            ("tax_document", "Tax Document"),
                            ("supporting_document", "Supporting Document"),
                        ],
                        max_length=30,
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vendor_invoice_documents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "invoice_documents",
                "ordering": ["-created_at"],
            },
        ),

        # ── Add vendor invoice fields to Invoice ────────────────────────────────
        migrations.AddField(
            model_name="invoice",
            name="vendor_invoice_number",
            field=models.CharField(
                blank=True, help_text="Vendor's own invoice reference number", max_length=255
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="invoice_date",
            field=models.DateField(
                blank=True, null=True, help_text="Date on the vendor's invoice"
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="due_date",
            field=models.DateField(blank=True, null=True, help_text="Payment due date"),
        ),
        migrations.AddField(
            model_name="invoice",
            name="subtotal_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Pre-tax subtotal",
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="tax_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Tax amount",
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="description",
            field=models.TextField(blank=True, help_text="Invoice description / notes"),
        ),
    ]
