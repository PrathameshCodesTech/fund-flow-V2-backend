from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db.models import Q

from apps.finance.models import FinanceHandoff
from apps.invoices.models import Invoice
from apps.vendors.models import Vendor


@dataclass
class MatchResult:
    status: str
    vendor: Vendor | None = None
    invoice: Invoice | None = None
    confidence: float = 0
    errors: list[str] = field(default_factory=list)


def _clean(value) -> str:
    return str(value or "").strip()


def _vendor_candidates(org_id: int, data: dict):
    qs = Vendor.objects.filter(org_id=org_id)
    vendor_code = _clean(data.get("vendor_code"))
    gstin = _clean(data.get("gstin"))
    email = _clean(data.get("vendor_email"))
    if vendor_code:
        return qs.filter(sap_vendor_id__iexact=vendor_code), "vendor_code"
    if gstin:
        return qs.filter(gstin__iexact=gstin), "gstin"
    if email:
        return qs.filter(Q(email__iexact=email) | Q(portal_email__iexact=email)), "vendor_email"
    return qs.none(), ""


def _invoice_candidates(org_id: int, data: dict, vendor: Vendor | None):
    invoice_number = _clean(data.get("invoice_number"))
    sap_document_number = _clean(data.get("sap_document_number"))
    qs = Invoice.objects.filter(scope_node__org_id=org_id).select_related("vendor")
    if vendor:
        qs = qs.filter(vendor=vendor)
    if invoice_number:
        return qs.filter(Q(vendor_invoice_number__iexact=invoice_number) | Q(title__iexact=invoice_number))
    if sap_document_number:
        invoice_ids = FinanceHandoff.objects.filter(
            org_id=org_id,
            module="invoice",
            subject_type="invoice",
            finance_reference_id__iexact=sap_document_number,
        ).values_list("subject_id", flat=True)
        return qs.filter(pk__in=invoice_ids)
    return qs.none()


def match_record_to_invoice(record) -> MatchResult:
    data = record.normalized_data or {}
    org_id = record.document.org_id
    vendors, vendor_basis = _vendor_candidates(org_id, data)
    vendor_count = vendors.count()
    if vendor_count > 1:
        return MatchResult("ambiguous", errors=[f"Multiple vendors matched by {vendor_basis}."])
    vendor = vendors.first() if vendor_count == 1 else None

    invoices = _invoice_candidates(org_id, data, vendor)
    invoice_count = invoices.count()
    if invoice_count == 0:
        return MatchResult(
            "unmatched",
            vendor=vendor,
            confidence=0.35 if vendor else 0,
            errors=["No invoice matched the extracted stable identifiers."],
        )
    if invoice_count > 1:
        return MatchResult(
            "ambiguous",
            vendor=vendor,
            confidence=0.4,
            errors=["Multiple invoices matched; finance review is required."],
        )

    invoice = invoices.first()
    if vendor and invoice.vendor_id and invoice.vendor_id != vendor.id:
        return MatchResult("conflict", vendor=vendor, invoice=invoice, errors=["Vendor and invoice ownership conflict."])

    errors = []
    amount = _clean(data.get("amount"))
    if amount:
        try:
            if Decimal(amount) != invoice.amount:
                errors.append(f"Extracted amount {amount} does not equal invoice amount {invoice.amount}.")
        except InvalidOperation:
            errors.append("Extracted amount is invalid.")
    currency = _clean(data.get("currency"))
    if currency and currency.upper() != invoice.currency.upper():
        errors.append(f"Extracted currency {currency} does not equal invoice currency {invoice.currency}.")
    if errors:
        return MatchResult("conflict", vendor=vendor or invoice.vendor, invoice=invoice, confidence=0.65, errors=errors)

    confidence = 0.95 if vendor and amount else 0.85 if vendor else 0.75
    return MatchResult("matched", vendor=vendor or invoice.vendor, invoice=invoice, confidence=confidence)

