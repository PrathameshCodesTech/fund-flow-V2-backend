import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


FIELD_ALIASES = {
    "vendorcode": "vendor_code",
    "sapvendorcode": "vendor_code",
    "sapvendorid": "vendor_code",
    "vendorid": "vendor_code",
    "vendorname": "vendor_name",
    "vendoremail": "vendor_email",
    "email": "vendor_email",
    "invoicenumber": "invoice_number",
    "invoiceno": "invoice_number",
    "invoiceref": "invoice_number",
    "sapdocumentnumber": "sap_document_number",
    "sapdocumentno": "sap_document_number",
    "documentnumber": "sap_document_number",
    "utr": "utr_number",
    "utrnumber": "utr_number",
    "paymentreference": "payment_reference_number",
    "paymentreferencenumber": "payment_reference_number",
    "bankreference": "bank_reference_number",
    "transactionid": "transaction_id",
    "amount": "amount",
    "paidamount": "amount",
    "invoiceamount": "amount",
    "totalamount": "amount",
    "currency": "currency",
    "invoicedate": "invoice_date",
    "paymentdate": "payment_date",
    "gstin": "gstin",
    "pan": "pan",
    "paymentmethod": "payment_method",
}


def canonical_key(value: object) -> str:
    compact = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    return FIELD_ALIASES.get(compact, compact)


def json_value(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value).strip()


def normalize_amount(value: object) -> str:
    cleaned = re.sub(r"[^0-9.\-]", "", str(value or "").replace(",", ""))
    try:
        return str(Decimal(cleaned).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return ""


def normalize_record(raw: dict) -> dict:
    normalized = {}
    for key, value in raw.items():
        canonical = canonical_key(key)
        if canonical in FIELD_ALIASES.values() and json_value(value):
            normalized[canonical] = json_value(value)
    if "amount" in normalized:
        normalized["amount"] = normalize_amount(normalized["amount"])
    if normalized.get("currency"):
        normalized["currency"] = normalized["currency"].upper()
    else:
        normalized["currency"] = "INR"
    return normalized


def infer_document_type(normalized: dict, filename: str = "") -> str:
    payment_markers = (
        normalized.get("utr_number"),
        normalized.get("payment_reference_number"),
        normalized.get("payment_date"),
        "payment" in filename.lower(),
        "remittance" in filename.lower(),
    )
    if any(payment_markers):
        return "payment_advice"
    if normalized.get("invoice_number") or "invoice" in filename.lower():
        return "invoice"
    return "unknown"


def confidence_for(normalized: dict, document_type: str) -> tuple[float, list[str]]:
    required = []
    strong = ["vendor_code", "gstin", "vendor_email", "vendor_name"]
    if document_type == "payment_advice":
        required += ["amount", "payment_date"]
        strong += ["utr_number", "payment_reference_number", "sap_document_number"]
    present_required = sum(bool(normalized.get(key)) for key in required)
    present_strong = sum(bool(normalized.get(key)) for key in strong)
    score = min(1.0, 0.2 + (present_required / max(len(required), 1)) * 0.55 + min(present_strong, 2) * 0.125)
    errors = [f"Missing extracted field: {key}." for key in required if not normalized.get(key)]
    if not normalized.get("invoice_number") and not normalized.get("sap_document_number"):
        errors.append("Missing extracted invoice number or SAP document number.")
        score = max(0, score - 0.25)
    if not any(normalized.get(key) for key in strong):
        errors.append("No stable vendor identifier was extracted.")
    return round(score, 4), errors
