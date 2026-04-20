from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
import logging
import re
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from apps.invoices.models import Invoice, InvoiceDocument, InvoiceStatus, VendorInvoiceSubmission, VendorInvoiceSubmissionStatus
from apps.access.models import PermissionAction, PermissionResource
from apps.access.services import user_has_permission_including_ancestors


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core invoice service (preserved from original)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_invoice(
    title,
    amount,
    currency,
    scope_node,
    created_by,
    po_number: str = "",
    vendor=None,
    enforce_permission: bool = True,
):
    """
    Create a new invoice if the creator has CREATE permission on INVOICE
    at scope_node or any ancestor.

    If vendor has po_mandate_enabled=True and po_number is not provided,
    raises InvoicePOMandateError.
    """
    if vendor and vendor.po_mandate_enabled and not po_number:
        raise InvoicePOMandateError(
            "This vendor requires a PO number on all invoices. "
            "Please provide 'po_number' and retry."
        )

    if enforce_permission and not user_has_permission_including_ancestors(
        created_by, PermissionAction.CREATE, PermissionResource.INVOICE, scope_node
    ):
        raise InvoicePermissionError(
            f"User {created_by} does not have create:invoice permission "
            f"at node {scope_node} or any ancestor."
        )
    return Invoice.objects.create(
        title=title,
        amount=amount,
        currency=currency,
        scope_node=scope_node,
        po_number=po_number,
        vendor=vendor,
        created_by=created_by,
        status=InvoiceStatus.DRAFT,
    )


# ---------------------------------------------------------------------------
# Public invoice services (preserved)
# ---------------------------------------------------------------------------

class InvoicePermissionError(PermissionError):
    """Raised when the actor lacks the required permission on an invoice operation."""


class InvoicePOMandateError(ValueError):
    """Raised when PO number is required but not provided."""


def sync_invoice_status(invoice, new_status):
    """Directly update invoice status. Used by workflow sync."""
    invoice.status = new_status
    invoice.save(update_fields=["status", "updated_at"])
    return invoice


# ---------------------------------------------------------------------------
# Extraction result
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    raw_cells: dict[str, Any]
    normalized: dict[str, Any]
    confidence: float
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Excel Invoice Extractor
# ---------------------------------------------------------------------------

# Known label → normalized field name
EXCEL_FIELD_MAP = {
    "invoice number": "vendor_invoice_number",
    "invoice #": "vendor_invoice_number",
    "inv #": "vendor_invoice_number",
    "inv number": "vendor_invoice_number",
    "invoice no": "vendor_invoice_number",
    "invoice no.": "vendor_invoice_number",
    "invoice date": "invoice_date",
    "inv date": "invoice_date",
    "date": "invoice_date",
    "due date": "due_date",
    "payment due date": "due_date",
    "po number": "po_number",
    "po #": "po_number",
    "po no": "po_number",
    "po no.": "po_number",
    "purchase order number": "po_number",
    "purchase order no": "po_number",
    "currency": "currency",
    "subtotal": "subtotal_amount",
    "sub total": "subtotal_amount",
    "amount": "subtotal_amount",
    "total before tax": "subtotal_amount",
    "subtotal amount": "subtotal_amount",
    "tax": "tax_amount",
    "tax amount": "tax_amount",
    "gst": "tax_amount",
    "gst amount": "tax_amount",
    "total": "total_amount",
    "grand total": "total_amount",
    "total amount": "total_amount",
    "invoice total": "total_amount",
    "total amount (inr)": "total_amount",
    "description": "description",
    "bill to": "bill_to_name",
    "billed to": "bill_to_name",
    "vendor name": "vendor_name",
    "gstin": "gstin",
    "gst in": "gstin",
    "pan": "pan",
    "invoice number *": "vendor_invoice_number",
    "invoice date *": "invoice_date",
    "due date *": "due_date",
    "po number *": "po_number",
    "currency *": "currency",
    "subtotal amount *": "subtotal_amount",
    "tax amount *": "tax_amount",
    "total amount *": "total_amount",
}


def _normalize_excel_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


def _try_parse_date(value: Any) -> str | None:
    """Parse a date value from Excel — returns ISO date string or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    # Try Excel serial number
    try:
        serial = float(s)
        if 1 <= serial <= 50000:
            from openpyxl.utils.datetime import from_excel
            dt = from_excel(serial)
            if dt:
                return dt.date().isoformat()
    except Exception:
        pass
    return None


def _try_parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace(" ", "")
    try:
        return Decimal(s)
    except Exception:
        return None


def _guess_field(key: str) -> str | None:
    norm = _normalize_excel_label(key)
    return EXCEL_FIELD_MAP.get(norm) or EXCEL_FIELD_MAP.get(key.lower().strip())


def _json_safe_dict(d: dict) -> dict:
    """Convert Decimal/other non-JSON-serializable values to JSON-safe types."""
    result = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            result[k] = float(v)
        elif isinstance(v, dict):
            result[k] = _json_safe_dict(v)
        elif isinstance(v, list):
            result[k] = [
                float(x) if isinstance(x, Decimal) else x for x in v
            ]
        else:
            result[k] = v
    return result


def extract_excel(file_obj) -> ExtractionResult:
    """
    Extract invoice fields from an Excel workbook (.xlsx/.xls).

    Scans all sheets for labelled rows.  Two layout patterns are supported:
      - key-value:  column-A = label, column-B = value
      - tabular:    first row = headers, subsequent rows = values
    """
    raw_cells: dict[str, Any] = {}
    normalized: dict[str, Any] = {}
    warnings: list[str] = []
    errors: list[str] = []
    confidence = 0.0
    found_fields = 0

    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_obj, data_only=True)
    except Exception as exc:
        errors.append(f"Could not open workbook: {exc}")
        return ExtractionResult(raw_cells={}, normalized={}, confidence=0.0, warnings=warnings, errors=errors)

    # Scan every sheet
    for sheet in wb.worksheets:
        if sheet.max_row < 1 or sheet.max_column < 1:
            continue

        # ── Pattern 1: key-value layout (label in col A) ───────────────────────
        for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 200), values_only=True):
            if not row:
                continue
            label_cell, value_cell = row[0], (row[1] if len(row) > 1 else None)
            if not label_cell:
                continue
            label = str(label_cell).strip()
            field_name = _guess_field(label)
            if field_name and value_cell is not None:
                # Skip if value_cell also looks like a field label
                # (prevents header-row mis-match: "Invoice Number" col B = "Invoice Date")
                if _guess_field(str(value_cell).strip()):
                    continue
                raw_cells[label] = value_cell
                # Parse value
                parsed = None
                if field_name in ("invoice_date", "due_date"):
                    parsed = _try_parse_date(value_cell)
                elif field_name in ("subtotal_amount", "tax_amount", "total_amount"):
                    parsed = _try_parse_decimal(value_cell)
                else:
                    parsed = str(value_cell).strip()
                if parsed:
                    normalized[field_name] = parsed
                    found_fields += 1

        # ── Pattern 2: tabular header row ──────────────────────────────────────
        header_row = None
        for row_idx, row in enumerate(sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 50), values_only=True), start=1):
            if not row or not row[0]:
                continue
            # Detect header: first cell looks like a field label
            if _guess_field(str(row[0])):
                header_row = [str(c).strip() if c else "" for c in row]
                break

        if header_row:
            # Scan values under header
            for row in sheet.iter_rows(min_row=row_idx + 1, max_row=min(sheet.max_row, row_idx + 100), values_only=True):
                if not row or not row[0]:
                    continue
                for col_idx, cell in enumerate(row):
                    if col_idx >= len(header_row):
                        break
                    header = header_row[col_idx]
                    field_name = _guess_field(header)
                    if field_name and cell is not None:
                        raw_cells[f"{header}:{row_idx}" if field_name == "total_amount" else header] = cell
                        parsed = None
                        if field_name in ("invoice_date", "due_date"):
                            parsed = _try_parse_date(cell)
                        elif field_name in ("subtotal_amount", "tax_amount", "total_amount"):
                            parsed = _try_parse_decimal(cell)
                        else:
                            parsed = str(cell).strip()
                        if parsed and field_name not in normalized:
                            normalized[field_name] = parsed
                            found_fields += 1

    # Compute confidence
    required_fields = ["vendor_invoice_number", "invoice_date", "total_amount", "currency"]
    matched = sum(1 for f in required_fields if f in normalized)
    confidence = matched / len(required_fields)

    if found_fields == 0:
        warnings.append("No invoice fields recognised in the uploaded file.")

    return ExtractionResult(
        raw_cells=raw_cells,
        normalized=normalized,
        confidence=min(1.0, confidence),
        warnings=warnings,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Submission services
# ---------------------------------------------------------------------------

class SubmissionStateError(ValueError):
    """Submission is in the wrong state for the requested operation."""


class DuplicateInvoiceError(ValueError):
    """A submission for the same vendor + invoice number already exists."""


def _file_hash(file_obj) -> str:
    """SHA-256 hash of a file object."""
    hasher = hashlib.sha256()
    for chunk in file_obj.chunks():
        hasher.update(chunk)
    # Reset file pointer for subsequent use
    file_obj.seek(0)
    return hasher.hexdigest()


def create_vendor_invoice_submission(
    *, user, vendor, scope_node, file_obj,
    normalized_data: dict | None = None,
) -> VendorInvoiceSubmission:
    """
    Create a new vendor invoice submission record from an uploaded file.

    Validates:
    - user has an active UserVendorAssignment to this vendor
    - vendor is active
    - scope_node is within vendor's scope_node hierarchy
    """
    from apps.vendors.models import OperationalStatus, UserVendorAssignment

    # Resolve vendor user assignment
    assignment = (
        UserVendorAssignment.objects
        .filter(user=user, is_active=True, vendor=vendor)
        .select_related("vendor__scope_node")
        .first()
    )
    if not assignment:
        raise SubmissionStateError("You are not linked to this vendor.")

    if vendor.operational_status != OperationalStatus.ACTIVE:
        raise SubmissionStateError("Your vendor account is not active.")

    # Scope node bounds check
    vendor_sn = vendor.scope_node
    if not (scope_node.path == vendor_sn.path or scope_node.path.startswith(vendor_sn.path + "/")):
        raise SubmissionStateError("This bill-to entity is outside your vendor scope.")

    # File info
    file_name = file_obj.name
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    file_type = "pdf" if ext == "pdf" else ("xlsx" if ext in ("xlsx", "xls") else ext)

    # Hash for duplicate detection
    file_obj.seek(0)
    file_hash = _file_hash(file_obj)
    file_obj.seek(0)

    submission = VendorInvoiceSubmission.objects.create(
        vendor=vendor,
        submitted_by=user,
        scope_node=scope_node,
        source_file=file_obj,
        source_file_name=file_name,
        source_file_type=file_type,
        source_file_hash=file_hash,
        normalized_data=normalized_data or {},
        status=VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
        if normalized_data
        else VendorInvoiceSubmissionStatus.UPLOADED,
    )
    return submission


def extract_invoice_submission(submission: VendorInvoiceSubmission) -> ExtractionResult:
    """
    Run extraction on the submission's source file and update the submission record.

    Returns an ExtractionResult; the submission's normalized_data and status are
    also updated atomically.
    """
    if submission.status not in (VendorInvoiceSubmissionStatus.UPLOADED, VendorInvoiceSubmissionStatus.NEEDS_CORRECTION):
        raise SubmissionStateError(
            f"Cannot extract — submission is in status '{submission.status}'."
        )

    submission.status = VendorInvoiceSubmissionStatus.EXTRACTING
    submission.save(update_fields=["status"])

    try:
        if submission.source_file_type == "pdf":
            # PDF V1: store raw text if PyPDF2 is available, otherwise flag needs_correction
            result = _extract_pdf_fallback(submission)
        else:
            file_obj = submission.source_file.open("rb")
            result = extract_excel(file_obj)
            file_obj.close()

        # Persist extraction results — convert Decimals to floats for JSON serialization
        submission.raw_extracted_data = _json_safe_dict(result.raw_cells)
        submission.normalized_data = _json_safe_dict(result.normalized)
        submission.confidence_score = Decimal(str(result.confidence)) if result.confidence else None
        if result.errors:
            submission.validation_errors = result.errors
            submission.status = VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
        elif result.confidence < 0.5:
            submission.status = VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
        else:
            submission.status = VendorInvoiceSubmissionStatus.READY
        submission.save(update_fields=[
            "raw_extracted_data", "normalized_data", "confidence_score",
            "validation_errors", "status", "updated_at",
        ])
        return result

    except Exception as exc:
        logger.exception("Extraction failed for submission %s", submission.pk)
        submission.status = VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
        submission.validation_errors = [{"extraction": str(exc)}]
        submission.save(update_fields=["status", "validation_errors"])
        return ExtractionResult(
            raw_cells={}, normalized={}, confidence=0.0,
            errors=[f"Extraction failed: {exc}"],
        )


def _extract_pdf_fallback(submission: VendorInvoiceSubmission) -> ExtractionResult:
    """
    V1 PDF fallback: attempt basic text extraction if PyPDF2 is available.
    If extraction yields nothing useful, return low-confidence result that
    forces the needs_correction flow.
    """
    warnings = []
    errors = []
    raw_text = ""
    try:
        import PyPDF2
        file_obj = submission.source_file.open("rb")
        reader = PyPDF2.PdfReader(file_obj)
        text_parts = []
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")
        raw_text = "\n".join(text_parts)
        file_obj.close()
    except ImportError:
        warnings.append("PDF text extraction library (PyPDF2) is not installed. Please fill in the details manually.")
    except Exception as exc:
        warnings.append(f"PDF text extraction failed: {exc}")

    # Try to extract fields via simple regex from raw text
    normalized = {}
    raw_cells = {"raw_text": raw_text}

    patterns = {
        "vendor_invoice_number": r"(?:invoice\s*(?:no\.?|number|#)[:\s]*)([A-Z0-9][-A-Z0-9/]*)",
        "invoice_date": r"(?:invoice\s*date[:\s]*)([\d]{1,4}[-/][\d]{1,2}[-/][\d]{1,4})",
        "po_number": r"(?:po\s*(?:no\.?|number|#)[:\s]*)([A-Z0-9][-A-Z0-9/]*)",
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, raw_text, re.IGNORECASE)
        if match:
            normalized[field] = match.group(1).strip()

    confidence = 0.0
    if normalized:
        confidence = 0.25  # Low confidence — manual review required

    if not normalized:
        warnings.append("No invoice fields could be extracted from the PDF. Please enter details manually.")
        errors.append("No fields could be extracted from the PDF.")

    return ExtractionResult(
        raw_cells=raw_cells,
        normalized=normalized,
        confidence=confidence,
        warnings=warnings,
        errors=errors,
    )


def validate_invoice_submission(submission: VendorInvoiceSubmission) -> list[dict]:
    """
    Run validation rules on normalized_data and return a list of error dicts.
    An empty list means validation passed.
    """
    errors = []
    nd = submission.normalized_data or {}
    vendor = submission.vendor

    # Invoice number
    if not nd.get("vendor_invoice_number"):
        errors.append({"field": "vendor_invoice_number", "message": "Invoice number is required."})
    else:
        # Duplicate check
        existing = VendorInvoiceSubmission.objects.filter(
            vendor=vendor,
            normalized_data__vendor_invoice_number=nd["vendor_invoice_number"],
        ).exclude(pk=submission.pk).exists()
        if existing:
            errors.append({"field": "vendor_invoice_number", "message": "An invoice with this number already exists."})

    # Invoice date
    if not nd.get("invoice_date"):
        errors.append({"field": "invoice_date", "message": "Invoice date is required."})

    # Total amount
    total = nd.get("total_amount")
    if not total:
        errors.append({"field": "total_amount", "message": "Total amount is required."})
    elif isinstance(total, (int, float, Decimal)) and total <= 0:
        errors.append({"field": "total_amount", "message": "Total amount must be greater than zero."})

    # Currency
    if not nd.get("currency"):
        errors.append({"field": "currency", "message": "Currency is required."})

    # PO number for PO-mandated vendors
    if vendor.po_mandate_enabled and not nd.get("po_number"):
        errors.append({"field": "po_number", "message": "PO number is required for this vendor."})

    return errors


def update_invoice_submission_fields(submission: VendorInvoiceSubmission, normalized_data: dict) -> VendorInvoiceSubmission:
    """Update normalized_data and re-validate. Returns updated submission."""
    submission.normalized_data = normalized_data
    submission.validation_errors = validate_invoice_submission(submission)
    submission.status = (
        VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
        if submission.validation_errors
        else VendorInvoiceSubmissionStatus.READY
    )
    submission.save(update_fields=["normalized_data", "validation_errors", "status", "updated_at"])
    return submission


@transaction.atomic
def submit_vendor_invoice_submission(submission: VendorInvoiceSubmission, user) -> Invoice:
    """
    Finalise a vendor invoice submission:
      1. Validate all required fields
      2. Create the final Invoice
      3. Attach documents
      4. Start workflow
      5. Update submission status
    """

    if submission.status not in (VendorInvoiceSubmissionStatus.UPLOADED, VendorInvoiceSubmissionStatus.NEEDS_CORRECTION, VendorInvoiceSubmissionStatus.READY):
        raise SubmissionStateError(
            f"Cannot submit — submission is in status '{submission.status}'."
        )

    # Run full validation
    validation_errors = validate_invoice_submission(submission)
    if validation_errors:
        submission.validation_errors = validation_errors
        submission.status = VendorInvoiceSubmissionStatus.NEEDS_CORRECTION
        submission.save(update_fields=["validation_errors", "status"])
        raise SubmissionStateError("Validation failed. Please correct the highlighted fields.")

    nd = submission.normalized_data

    # Build amount (prefer total_amount; compute from subtotal + tax if missing)
    total = nd.get("total_amount")
    if not total:
        subtotal = float(nd.get("subtotal_amount") or 0)
        tax = float(nd.get("tax_amount") or 0)
        total = Decimal(str(subtotal + tax))
    else:
        total = Decimal(str(total))

    currency = nd.get("currency", "INR")
    invoice_date_str = nd.get("invoice_date")
    due_date_str = nd.get("due_date")

    from django.utils.dateparse import parse_date
    invoice_date = parse_date(invoice_date_str) if invoice_date_str else None
    due_date = parse_date(due_date_str) if due_date_str else None

    # Create final Invoice — stops at pending_workflow; no auto workflow start.
    invoice = Invoice.objects.create(
        scope_node=submission.scope_node,
        title=nd.get("vendor_invoice_number", "Untitled Invoice"),
        amount=total,
        currency=currency,
        vendor=submission.vendor,
        created_by=user,
        po_number=nd.get("po_number", ""),
        vendor_invoice_number=nd.get("vendor_invoice_number", ""),
        invoice_date=invoice_date,
        due_date=due_date,
        subtotal_amount=Decimal(str(nd.get("subtotal_amount", 0))),
        tax_amount=Decimal(str(nd.get("tax_amount", 0))),
        description=nd.get("description", ""),
        status=InvoiceStatus.PENDING_WORKFLOW,
    )

    # Link documents to invoice
    for doc in submission.documents.all():
        doc.invoice = invoice
        doc.save(update_fields=["invoice"])

    # Update submission
    submission.final_invoice = invoice
    submission.status = VendorInvoiceSubmissionStatus.SUBMITTED
    submission.submitted_at = timezone.now()
    submission.save(update_fields=["final_invoice", "status", "submitted_at"])

    return invoice


def cancel_vendor_invoice_submission(submission: VendorInvoiceSubmission) -> None:
    """
    Cancel a vendor invoice submission.
    Allowed from: uploaded, extracting (blocked), needs_correction, ready.
    Not allowed from: submitted, cancelled.
    Not allowed if a final invoice already exists.
    """
    if submission.final_invoice_id is not None:
        raise SubmissionStateError(
            "Cannot cancel — a final invoice has already been created from this submission."
        )

    # extracting is a transitional state — block cancellation while actively extracting
    if submission.status == VendorInvoiceSubmissionStatus.EXTRACTING:
        raise SubmissionStateError(
            "Cannot cancel — extraction is in progress. Please wait for it to complete."
        )

    cancellable = (
        VendorInvoiceSubmissionStatus.UPLOADED,
        VendorInvoiceSubmissionStatus.NEEDS_CORRECTION,
        VendorInvoiceSubmissionStatus.READY,
    )
    if submission.status == VendorInvoiceSubmissionStatus.SUBMITTED:
        raise SubmissionStateError("Cannot cancel — submission has already been submitted.")
    if submission.status == VendorInvoiceSubmissionStatus.CANCELLED:
        raise SubmissionStateError("Submission is already cancelled.")
    if submission.status not in cancellable:
        raise SubmissionStateError(
            f"Cannot cancel — submission is in status '{submission.status}'."
        )

    submission.status = VendorInvoiceSubmissionStatus.CANCELLED
    submission.save(update_fields=["status", "updated_at"])


# ---------------------------------------------------------------------------
# Invoice template downloads
# ---------------------------------------------------------------------------

def generate_excel_template() -> HttpResponse:
    """
    Generate a Vendor Invoice Upload Template .xlsx with labelled header row.
    Labels match extract_excel() field detection so uploaded files work directly.
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        return HttpResponse("openpyxl not installed", status=500)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice Data"

    # Header style
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1E3A5F")
    header_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    headers = [
        "Invoice Number", "Invoice Date", "Due Date", "PO Number",
        "Currency", "Subtotal Amount", "Tax Amount", "Total Amount",
        "Description",
    ]
    ws.append(headers)

    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    # Column widths
    col_widths = [22, 16, 16, 18, 10, 18, 14, 16, 40]
    for idx, width in enumerate(col_widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = width

    ws.freeze_panes = "A2"
    ws.append([])  # blank row hint
    ws.append(["INV-001", "2026-04-18", "2026-05-18", "PO-001", "INR", "10000", "1800", "11800", "Services rendered"])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = "attachment; filename=Vendor_Invoice_Template.xlsx"
    return response


def generate_pdf_template() -> HttpResponse:
    """
    Generate a styled Vendor Invoice Upload Template PDF.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.colors import HexColor, white, black
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
        )
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
    except ImportError:
        return _pdf_template_fallback()

    # ── Colours ────────────────────────────────────────────────────────────────
    NAVY      = HexColor("#1E3A5F")
    LIGHT_BLUE = HexColor("#E8F0F8")
    MID_BLUE  = HexColor("#4A7BAD")
    GREEN     = HexColor("#2E7D32")
    RED       = HexColor("#C62828")
    GREY_BG   = HexColor("#F5F5F5")
    BORDER    = HexColor("#B0BEC5")

    # ── Document ───────────────────────────────────────────────────────────────
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=18*mm, bottomMargin=18*mm,
    )

    # ── Styles ─────────────────────────────────────────────────────────────────
    def ps(name, **kw):
        return ParagraphStyle(name, **kw)

    title_s = ps("title", fontSize=18, leading=22, textColor=white, alignment=TA_CENTER, fontName="Helvetica-Bold")
    subtitle_s = ps("subtitle", fontSize=9, leading=12, textColor=LIGHT_BLUE, alignment=TA_CENTER, fontName="Helvetica")
    section_s = ps("section", fontSize=8, leading=10, textColor=MID_BLUE, fontName="Helvetica-Bold")
    label_s = ps("label", fontSize=8.5, leading=11, textColor=NAVY, fontName="Helvetica-Bold")
    hint_s  = ps("hint",  fontSize=7.5, leading=10, textColor=HexColor("#607D8B"), fontName="Helvetica-Oblique")
    body_s  = ps("body",  fontSize=8,   leading=11, textColor=black,  fontName="Helvetica")
    note_s  = ps("note",  fontSize=7.5, leading=10, textColor=HexColor("#455A64"), fontName="Helvetica")
    footer_s = ps("footer", fontSize=6.5, leading=9, textColor=HexColor("#90A4AE"), alignment=TA_CENTER)

    # ── Build elements ─────────────────────────────────────────────────────────
    els = []

    # ── Header banner ──────────────────────────────────────────────────────────
    header_data = [[
        Paragraph("FundFlow — Vendor Invoice Submission Template", title_s),
    ]]
    header_tbl = Table(header_data, colWidths=[174*mm])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",   (0, 0), (-1, -1), 8*mm),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8*mm),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
        ("ROUNDEDCORNERS", [4]),
    ]))
    els.append(header_tbl)
    els.append(Spacer(1, 3*mm))

    # ── Recommended note ───────────────────────────────────────────────────────
    note_data = [[
        Paragraph(
            "&#10004; Recommended: Use the <b>Excel template</b> for auto-fill. "
            "Upload this completed PDF alongside your invoice for seamless processing.",
            note_s,
        )
    ]]
    note_tbl = Table(note_data, colWidths=[174*mm])
    note_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), HexColor("#E8F5E9")),
        ("TOPPADDING",   (0, 0), (-1, -1), 3*mm),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3*mm),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
        ("BOX",          (0, 0), (-1, -1), 0.5, GREEN),
    ]))
    els.append(note_tbl)
    els.append(Spacer(1, 5*mm))

    # ── Section header helper ──────────────────────────────────────────────────
    def section_row(label):
        return [Paragraph(label.upper(), section_s)]

    # ── Section: Invoice Details ───────────────────────────────────────────────
    els.append(Paragraph("Invoice Details", section_s))
    els.append(Spacer(1, 2*mm))

    field_label = lambda t: Paragraph(t, label_s)
    field_hint  = lambda t: Paragraph(t, hint_s)

    # We'll build a 2-col table: [Field Name | Fillable space]
    def row(label, hint="", required=False):
        req_mark = " <font color='#C62828'>*</font>" if required else ""
        return [field_label(label + req_mark), field_hint(hint) if hint else Paragraph("", hint_s)]

    DETAIL_FIELDS = [
        ("Invoice Number", "e.g. INV-2026-001", True),
        ("Invoice Date", "YYYY-MM-DD  (e.g. 2026-04-18)", True),
        ("Due Date", "YYYY-MM-DD", False),
        ("Currency", "e.g. INR, USD, EUR — use 3-letter ISO code", False),
        ("Description", "Brief description of goods or services", False),
    ]

    detail_rows = [row(l, h, r) for l, h, r in DETAIL_FIELDS]

    detail_tbl = Table(detail_rows, colWidths=[45*mm, 129*mm])
    detail_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (0, -1), LIGHT_BLUE),
        ("BACKGROUND",   (1, 0), (1, -1), white),
        ("BOX",          (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID",    (0, 0), (-1, -1), 0.25, BORDER),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3*mm),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3*mm),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3*mm),
    ]))
    els.append(detail_tbl)
    els.append(Spacer(1, 5*mm))

    # ── Section: Amounts ───────────────────────────────────────────────────────
    els.append(Paragraph("Amounts", section_s))
    els.append(Spacer(1, 2*mm))

    AMOUNT_FIELDS = [
        ("Subtotal Amount", "Numeric value without currency symbol (e.g. 10000)", True),
        ("Tax Amount", "Tax portion (e.g. 1800). Total = Subtotal + Tax.", False),
        ("Total Amount", "Grand total = Subtotal + Tax. Auto-calculated if using Excel.", False),
        ("PO Number", "Required if your vendor account has PO mandate enabled.", False),
    ]

    amount_rows = [row(l, h, r) for l, h, r in AMOUNT_FIELDS]

    amount_tbl = Table(amount_rows, colWidths=[45*mm, 129*mm])
    amount_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (0, -1), LIGHT_BLUE),
        ("BACKGROUND",   (1, 0), (1, -1), white),
        ("BOX",          (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID",    (0, 0), (-1, -1), 0.25, BORDER),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3*mm),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3*mm),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3*mm),
    ]))
    els.append(amount_tbl)
    els.append(Spacer(1, 5*mm))

    # ── PO Mandate notice ───────────────────────────────────────────────────────
    po_notice_data = [[
        Paragraph(
            "<b>Note:</b> The <b>PO Number</b> field above is only mandatory if your vendor account "
            "has PO mandate enabled. Check your vendor profile or contact support to confirm.",
            note_s,
        )
    ]]
    po_notice_tbl = Table(po_notice_data, colWidths=[174*mm])
    po_notice_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), HexColor("#FFF8E1")),
        ("TOPPADDING",   (0, 0), (-1, -1), 3*mm),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3*mm),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4*mm),
        ("BOX",          (0, 0), (-1, -1), 0.5, HexColor("#F9A825")),
    ]))
    els.append(po_notice_tbl)
    els.append(Spacer(1, 5*mm))

    # ── Submission instructions ────────────────────────────────────────────────
    els.append(Paragraph("How to Submit", section_s))
    els.append(Spacer(1, 2*mm))

    steps_data = [
        [Paragraph("1", label_s), Paragraph("Fill in all fields. Print or save this PDF.", body_s)],
        [Paragraph("2", label_s), Paragraph("Log in to the Vendor Portal and choose <b>Upload Invoice</b>.", body_s)],
        [Paragraph("3", label_s), Paragraph("Attach your invoice file (PDF/Excel) and this completed template.", body_s)],
        [Paragraph("4", label_s), Paragraph("Submit — our system will auto-extract data and notify you of status.", body_s)],
    ]
    steps_tbl = Table(steps_data, colWidths=[10*mm, 164*mm])
    steps_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (0, -1), MID_BLUE),
        ("BACKGROUND",   (1, 0), (1, -1), GREY_BG),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2.5*mm),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2.5*mm),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3*mm),
        ("INNERGRID",    (0, 0), (-1, -1), 0.25, BORDER),
        ("BOX",          (0, 0), (-1, -1), 0.5, BORDER),
        ("ALIGN",        (0, 0), (0, -1), "CENTER"),
        ("TEXTCOLOR",    (0, 0), (0, -1), white),
    ]))
    els.append(steps_tbl)
    els.append(Spacer(1, 6*mm))

    # ── Divider + footer ───────────────────────────────────────────────────────
    els.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    els.append(Spacer(1, 2*mm))
    els.append(Paragraph(
        "FundFlow Vendor Portal — Template generated automatically. "
        "For support contact your assigned procurement manager.",
        footer_s,
    ))

    # ── Build ───────────────────────────────────────────────────────────────────
    doc.build(els)
    buffer.seek(0)

    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = "attachment; filename=Vendor_Invoice_Template.pdf"
    return response


def _pdf_template_fallback() -> HttpResponse:
    """Plain-text fallback when reportlab is not installed."""
    content = (
        "VENDOR INVOICE SUBMISSION TEMPLATE\n"
        "=" * 50 + "\n\n"
        "Fields to fill in:\n"
        "  Invoice Number  : _______________\n"
        "  Invoice Date    : _______________  (YYYY-MM-DD)\n"
        "  Due Date        : _______________  (YYYY-MM-DD)\n"
        "  PO Number        : _______________\n"
        "  Currency        : _______________  (e.g. INR)\n"
        "  Subtotal Amount  : _______________\n"
        "  Tax Amount       : _______________\n"
        "  Total Amount     : _______________\n"
        "  Description       : _______________\n\n"
        "Note: PO Number is required only if your vendor account has PO mandate enabled.\n"
        "Save this file and re-upload it to the Vendor Portal."
    )
    response = HttpResponse(content, content_type="text/plain")
    response["Content-Disposition"] = "attachment; filename=Vendor_Invoice_Template.txt"
    return response
