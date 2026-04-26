from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.models import NodeType, Organization, ScopeNode
from apps.invoices.models import VendorInvoiceSubmission, VendorInvoiceSubmissionStatus
from apps.invoices.services import _extract_pdf_template_values, extract_invoice_submission
from apps.users.models import User
from apps.vendors.models import OperationalStatus, Vendor


PDF_TEMPLATE_RAW_TEXT = """FundFlow  Vendor Invoice Submission Template
 Recommended: Use the Excel template for auto-fill. Upload this completed PDF alongside your invoice for seamless processing.
Invoice Details
Invoice Number *
e.g. INV-2026-001
Invoice Date *
YYYY-MM-DD (e.g. 2026-04-18)
Due Date
YYYY-MM-DD
Currency
e.g. INR, USD, EUR  use 3-letter ISO code
Description
Brief description of goods or services
Amounts
Subtotal Amount *
Numeric value without currency symbol (e.g. 10000)
Tax Amount
Tax portion (e.g. 1800). Total = Subtotal + Tax.
Total Amount
Grand total = Subtotal + Tax. Auto-calculated if using Excel.
PO Number
Required if your vendor account has PO mandate enabled.
Note: The PO Number field above is only mandatory if your vendor account has PO mandate enabled. Check your vendor profile or contact
support to confirm.
How to Submit
1
Fill in all fields. Print or save this PDF.
2
Log in to the Vendor Portal and choose Upload Invoice.
3
Attach your invoice file (PDF/Excel) and this completed template.
4
Submit  our system will auto-extract data and notify you of status.
 FundFlow Vendor Portal  Template generated automatically. For support contact your assigned procurement manager.
 INV-2026-047
2026-04-24
2026-05-08
INR
Web development and backend API integration services
50000
9000
59000
FF-PO-2026-112
Completed Sample
"""


def test_extract_pdf_template_values_parses_generated_fillable_template():
    normalized = _extract_pdf_template_values(PDF_TEMPLATE_RAW_TEXT)

    assert normalized == {
        "vendor_invoice_number": "INV-2026-047",
        "invoice_date": "2026-04-24",
        "due_date": "2026-05-08",
        "currency": "INR",
        "description": "Web development and backend API integration services",
        "subtotal_amount": 50000,
        "tax_amount": 9000,
        "total_amount": 59000,
        "po_number": "FF-PO-2026-112",
    }


def test_extract_invoice_submission_marks_pdf_ready(monkeypatch, db):
    class _FakePage:
        def extract_text(self):
            return PDF_TEMPLATE_RAW_TEXT

    class _FakeReader:
        def __init__(self, _file_obj):
            self.pages = [_FakePage()]

    monkeypatch.setitem(__import__("sys").modules, "PyPDF2", SimpleNamespace(PdfReader=_FakeReader))

    org = Organization.objects.create(name="PDF Org", code="pdf-org")
    company = ScopeNode.objects.create(
        org=org,
        parent=None,
        name="HQ",
        code="hq",
        node_type=NodeType.COMPANY,
        path="/pdf-org/hq",
        depth=0,
    )
    vendor = Vendor.objects.create(
        org=org,
        scope_node=company,
        vendor_name="PDF Vendor",
        sap_vendor_id="SAP-PDF-1",
        operational_status=OperationalStatus.ACTIVE,
    )
    user = User.objects.create_user(email="pdf-vendor@example.com", password="pass")
    submission = VendorInvoiceSubmission.objects.create(
        vendor=vendor,
        submitted_by=user,
        scope_node=company,
        status=VendorInvoiceSubmissionStatus.UPLOADED,
        source_file=SimpleUploadedFile("filled-template.pdf", b"%PDF-1.4 fake"),
        source_file_name="filled-template.pdf",
        source_file_type="pdf",
    )

    result = extract_invoice_submission(submission)
    submission.refresh_from_db()

    assert submission.status == VendorInvoiceSubmissionStatus.READY
    assert result.confidence == 1.0
    assert submission.normalized_data["vendor_invoice_number"] == "INV-2026-047"
    assert submission.normalized_data["currency"] == "INR"
    assert submission.normalized_data["total_amount"] == 59000.0
    assert submission.normalized_data["po_number"] == "FF-PO-2026-112"
