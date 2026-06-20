import io
import tempfile
from pathlib import Path
from django.test import TestCase, override_settings
from openpyxl import Workbook

from apps.core.models import NodeType, Organization, ScopeNode
from apps.document_ingestion.extractors import extract_document
from apps.document_ingestion.models import (
    ExternalDocumentImport,
    ExternalDocumentSource,
    ExternalDocumentStatus,
    ExternalDocumentType,
    MatchStatus,
)
from apps.document_ingestion.services import apply_payment_record, poll_source, process_document, register_document
from apps.invoices.models import Invoice, InvoiceStatus
from apps.users.models import User
from apps.vendors.models import MarketingStatus, OperationalStatus, Vendor


class DocumentIngestionServiceTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media_dir.cleanup)
        self.org = Organization.objects.create(name="Horizon", code="horizon")
        self.scope = ScopeNode.objects.create(
            org=self.org,
            parent=None,
            name="Marketing",
            code="marketing",
            node_type=NodeType.DEPARTMENT,
            path="/horizon/marketing",
            depth=0,
        )
        self.actor = User.objects.create_superuser("finance@example.com", "test-password")
        self.vendor = Vendor.objects.create(
            org=self.org,
            scope_node=self.scope,
            vendor_name="Acme Supplies",
            email="accounts@acme.example",
            sap_vendor_id="V100",
            gstin="27ABCDE1234F1Z5",
            marketing_status=MarketingStatus.APPROVED,
            operational_status=OperationalStatus.ACTIVE,
        )
        self.invoice = Invoice.objects.create(
            scope_node=self.scope,
            title="INV-100",
            vendor_invoice_number="INV-100",
            vendor=self.vendor,
            amount="41300.00",
            currency="INR",
            status=InvoiceStatus.FINANCE_APPROVED,
            created_by=self.actor,
        )

    def _payment_text(self):
        return (
            b"Payment Advice\nVendor Code: V100\nInvoice Number: INV-100\n"
            b"Paid Amount: 41,300.00\nPayment Date: 20-06-2026\nUTR Number: UTR-9001\n"
        )

    def test_text_payment_is_extracted_matched_and_applied_through_invoice_service(self):
        document = register_document(
            org=self.org,
            filename="payment-advice.txt",
            content=self._payment_text(),
            actor=self.actor,
        )
        process_document(document, actor=self.actor)
        document.refresh_from_db()
        record = document.records.get()
        self.assertEqual(document.status, ExternalDocumentStatus.MATCHED)
        self.assertEqual(record.document_type, ExternalDocumentType.PAYMENT_ADVICE)
        self.assertEqual(record.match_status, MatchStatus.MATCHED)
        self.assertEqual(record.matched_invoice, self.invoice)

        apply_payment_record(record, actor=self.actor)
        self.invoice.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.PAID)
        self.assertEqual(record.applied_payment.utr_number, "UTR-9001")
        self.assertEqual(record.applied_payment.paid_amount, self.invoice.amount)

    def test_duplicate_hash_is_recorded_without_reprocessing(self):
        first = register_document(org=self.org, filename="one.txt", content=self._payment_text())
        second = register_document(org=self.org, filename="two.txt", content=self._payment_text())
        self.assertEqual(second.status, ExternalDocumentStatus.DUPLICATE)
        self.assertEqual(second.duplicate_of, first)
        self.assertEqual(ExternalDocumentImport.objects.count(), 2)

    def test_amount_mismatch_requires_review_and_cannot_be_applied(self):
        content = self._payment_text().replace(b"41,300.00", b"40,000.00")
        document = register_document(org=self.org, filename="wrong-amount.txt", content=content)
        process_document(document, actor=self.actor)
        document.refresh_from_db()
        record = document.records.get()
        self.assertEqual(document.status, ExternalDocumentStatus.REVIEW_REQUIRED)
        self.assertEqual(record.match_status, MatchStatus.CONFLICT)
        self.assertIn("does not equal invoice amount", " ".join(record.validation_errors))
        with self.assertRaisesMessage(ValueError, "validated invoice match"):
            apply_payment_record(record, actor=self.actor)

    def test_excel_extraction_creates_one_record_per_data_row(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Vendor Code", "Invoice Number", "Paid Amount", "Payment Date", "UTR Number"])
        sheet.append(["V100", "INV-100", 41300, "20-06-2026", "UTR-1"])
        sheet.append(["V200", "INV-200", 1000, "20-06-2026", "UTR-2"])
        stream = io.BytesIO()
        workbook.save(stream)
        result = extract_document(stream.getvalue(), "payments.xlsx")
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].normalized_data["invoice_number"], "INV-100")

    def test_local_source_poll_processes_and_archives_each_file(self):
        root = Path(self.media_dir.name) / "drop"
        inbox = root / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "payment.txt").write_bytes(self._payment_text())
        source = ExternalDocumentSource.objects.create(
            org=self.org,
            name="Local finance drop",
            connector_type="local",
            config_key="TEST_LOCAL",
            base_path=str(root),
            public_config={"inbox": "inbox", "archive": "archive", "quarantine": "quarantine"},
        )
        result = poll_source(source, actor=self.actor)
        self.assertEqual(len(result.documents), 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.documents[0].status, ExternalDocumentStatus.MATCHED)
        self.assertFalse((inbox / "payment.txt").exists())
        self.assertTrue((root / "archive" / "payment.txt").exists())
