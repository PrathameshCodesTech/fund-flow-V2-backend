import hashlib
import mimetypes
from datetime import date, datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.files.base import ContentFile
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.document_ingestion.connectors import build_connector
from apps.document_ingestion.extractors import ExtractionError, extract_document
from apps.document_ingestion.matching import match_record_to_invoice
from apps.document_ingestion.models import (
    ExternalDocumentEvent,
    ExternalDocumentImport,
    ExternalDocumentRecord,
    ExternalDocumentStatus,
    ExternalDocumentType,
    MatchStatus,
)


class IngestionError(ValueError):
    pass


@dataclass
class PollResult:
    documents: list = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _event(document, event_type, *, actor=None, from_status="", to_status="", message="", metadata=None):
    return ExternalDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        message=message,
        metadata=metadata or {},
        actor=actor,
    )


def _set_status(document, status, *, actor=None, event_type="status_changed", message=""):
    previous = document.status
    document.status = status
    document.save(update_fields=["status", "updated_at"])
    _event(document, event_type, actor=actor, from_status=previous, to_status=status, message=message)


@transaction.atomic
def register_document(*, org, filename, content: bytes, source=None, remote_identifier="", remote_path="", actor=None):
    if not content:
        raise IngestionError("Document is empty.")
    max_bytes = getattr(settings, "DOCUMENT_INGESTION_MAX_FILE_SIZE_MB", 25) * 1024 * 1024
    if len(content) > max_bytes:
        raise IngestionError(f"Document exceeds the {max_bytes // (1024 * 1024)} MB size limit.")
    digest = hashlib.sha256(content).hexdigest()
    duplicate = ExternalDocumentImport.objects.filter(org=org, content_hash=digest).order_by("created_at").first()
    document = ExternalDocumentImport(
        org=org,
        source=source,
        remote_identifier=remote_identifier,
        remote_path=remote_path,
        original_filename=Path(filename).name,
        content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        file_size=len(content),
        content_hash=digest,
        duplicate_of=duplicate,
        created_by=actor,
        downloaded_at=timezone.now(),
        status=ExternalDocumentStatus.DUPLICATE if duplicate else ExternalDocumentStatus.DOWNLOADED,
    )
    document.source_file.save(document.original_filename, ContentFile(content), save=False)
    document.save()
    _event(
        document,
        "document_registered",
        actor=actor,
        to_status=document.status,
        metadata={"sha256": digest, "duplicate_of": duplicate.pk if duplicate else None},
    )
    return document


def process_document(document, *, actor=None, force=False):
    with transaction.atomic():
        document = ExternalDocumentImport.objects.select_for_update().get(pk=document.pk)
        if document.status == ExternalDocumentStatus.DUPLICATE and not force:
            return document
        if document.status == ExternalDocumentStatus.APPLIED and not force:
            raise IngestionError("An applied document cannot be reprocessed.")
        document.processing_attempts += 1
        document.last_error = ""
        document.save(update_fields=["processing_attempts", "last_error", "updated_at"])
    try:
        document.source_file.open("rb")
        content = document.source_file.read()
        document.source_file.close()
        result = extract_document(content, document.original_filename)
        if not result.records:
            raise ExtractionError("No business records were found in the document.")
        with transaction.atomic():
            document = ExternalDocumentImport.objects.select_for_update().get(pk=document.pk)
            document.records.all().delete()
            created_records = []
            for index, extracted in enumerate(result.records, start=1):
                created_records.append(
                    ExternalDocumentRecord(
                        document=document,
                        record_index=index,
                        document_type=extracted.document_type,
                        raw_data=extracted.raw_data,
                        normalized_data=extracted.normalized_data,
                        confidence_score=extracted.confidence_score,
                        validation_errors=extracted.validation_errors,
                    )
                )
            ExternalDocumentRecord.objects.bulk_create(created_records)
            types = {record.document_type for record in created_records}
            document.document_type = types.pop() if len(types) == 1 else ExternalDocumentType.UNKNOWN
            document.raw_extracted_data = result.raw_data
            document.normalized_data = {"extractor": result.extractor, "record_count": len(created_records)}
            document.validation_errors = []
            document.extracted_at = timezone.now()
            document.status = ExternalDocumentStatus.EXTRACTED
            document.save(update_fields=[
                "document_type", "raw_extracted_data", "normalized_data", "validation_errors",
                "extracted_at", "status", "updated_at",
            ])
            _event(document, "document_extracted", actor=actor, to_status=document.status, metadata=document.normalized_data)
    except Exception as exc:
        with transaction.atomic():
            document = ExternalDocumentImport.objects.select_for_update().get(pk=document.pk)
            document.status = ExternalDocumentStatus.FAILED
            document.last_error = str(exc)
            document.validation_errors = [str(exc)]
            document.save(update_fields=["status", "last_error", "validation_errors", "updated_at"])
            _event(document, "extraction_failed", actor=actor, to_status=document.status, message=str(exc))
        return document
    return match_document(document, actor=actor)


@transaction.atomic
def quarantine_document(document, *, actor=None, reason="Manually quarantined."):
    document = ExternalDocumentImport.objects.select_for_update().get(pk=document.pk)
    document.last_error = reason
    document.save(update_fields=["last_error", "updated_at"])
    _set_status(
        document,
        ExternalDocumentStatus.QUARANTINED,
        actor=actor,
        event_type="document_quarantined",
        message=reason,
    )
    return document


@transaction.atomic
def match_document(document, *, actor=None):
    document = ExternalDocumentImport.objects.select_for_update().get(pk=document.pk)
    records = list(document.records.select_related("document__org"))
    if not records:
        raise IngestionError("Extract the document before matching it.")
    statuses = []
    for record in records:
        result = match_record_to_invoice(record)
        record.match_status = result.status
        record.matched_vendor = result.vendor
        record.matched_invoice = result.invoice
        record.confidence_score = max(Decimal(str(result.confidence)), record.confidence_score or Decimal("0"))
        record.validation_errors = list(dict.fromkeys([*(record.validation_errors or []), *result.errors]))
        record.matched_at = timezone.now()
        record.reviewed_by = actor
        record.save(update_fields=[
            "match_status", "matched_vendor", "matched_invoice", "confidence_score",
            "validation_errors", "matched_at", "reviewed_by", "updated_at",
        ])
        statuses.append(result.status)
    if all(status == MatchStatus.MATCHED for status in statuses):
        target_status = ExternalDocumentStatus.MATCHED
    else:
        target_status = ExternalDocumentStatus.REVIEW_REQUIRED
    _set_status(document, target_status, actor=actor, event_type="document_matched")
    return document


def correct_record(record, *, normalized_data, document_type=None, actor=None):
    with transaction.atomic():
        record = ExternalDocumentRecord.objects.select_for_update().select_related("document").get(pk=record.pk)
        record.normalized_data = normalized_data
        if document_type:
            record.document_type = document_type
        record.validation_errors = []
        record.reviewed_by = actor
        record.save(update_fields=[
            "normalized_data", "document_type", "validation_errors", "reviewed_by", "updated_at",
        ])
        _event(
            record.document,
            "record_corrected",
            actor=actor,
            metadata={"record_id": record.pk, "document_type": record.document_type},
        )
        document = record.document
    match_document(document, actor=actor)
    record.refresh_from_db()
    return record


@transaction.atomic
def manually_link_record(record, *, invoice, actor):
    record = ExternalDocumentRecord.objects.select_for_update().select_related("document").get(pk=record.pk)
    if invoice.scope_node.org_id != record.document.org_id:
        raise IngestionError("Invoice belongs to a different organization.")
    data = record.normalized_data or {}
    errors = []
    amount = str(data.get("amount") or "").strip()
    if amount:
        try:
            if Decimal(amount) != invoice.amount:
                errors.append("Extracted amount does not equal the selected invoice amount.")
        except InvalidOperation:
            errors.append("Extracted amount is invalid.")
    currency = str(data.get("currency") or "").strip().upper()
    if currency and currency != invoice.currency.upper():
        errors.append("Extracted currency does not equal the selected invoice currency.")
    if errors:
        raise IngestionError(" ".join(errors))
    record.matched_invoice = invoice
    record.matched_vendor = invoice.vendor
    record.match_status = MatchStatus.MATCHED
    record.confidence_score = Decimal("1.0000")
    record.validation_errors = []
    record.matched_at = timezone.now()
    record.reviewed_by = actor
    record.save(update_fields=[
        "matched_invoice", "matched_vendor", "match_status", "confidence_score",
        "validation_errors", "matched_at", "reviewed_by", "updated_at",
    ])
    if not record.document.records.exclude(match_status=MatchStatus.MATCHED).exists():
        _set_status(record.document, ExternalDocumentStatus.MATCHED, actor=actor, event_type="document_manually_matched")
    _event(
        record.document,
        "record_manually_linked",
        actor=actor,
        metadata={"record_id": record.pk, "invoice_id": invoice.pk},
    )
    return record


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@transaction.atomic
def apply_payment_record(record, *, actor):
    from apps.invoices.models import InvoicePaymentStatus, PaymentMethod
    from apps.invoices.services import record_invoice_payment

    record = ExternalDocumentRecord.objects.select_for_update().select_related("document", "matched_invoice").get(pk=record.pk)
    if record.document_type != ExternalDocumentType.PAYMENT_ADVICE:
        raise IngestionError("Only payment advice records can update invoice payment status.")
    if record.match_status != MatchStatus.MATCHED or not record.matched_invoice_id:
        raise IngestionError("Record must have one validated invoice match before payment can be applied.")
    if record.applied_payment_id:
        return record
    data = record.normalized_data or {}
    try:
        amount = Decimal(str(data.get("amount", "")))
    except InvalidOperation as exc:
        raise IngestionError("A valid paid amount is required.") from exc
    payment_date = _parse_date(data.get("payment_date"))
    if not payment_date:
        raise IngestionError("A valid payment date is required.")
    utr = str(data.get("utr_number") or "").strip()
    if not utr:
        raise IngestionError("UTR number is required before applying a payment.")
    method = str(data.get("payment_method") or "").strip().lower()
    allowed_methods = {choice for choice, _ in PaymentMethod.choices}
    if method not in allowed_methods:
        method = PaymentMethod.OTHER
    payment = record_invoice_payment(
        record.matched_invoice,
        actor,
        {
            "payment_status": InvoicePaymentStatus.PAID,
            "payment_method": method,
            "payment_reference_number": str(data.get("payment_reference_number") or ""),
            "utr_number": utr,
            "transaction_id": str(data.get("transaction_id") or ""),
            "bank_reference_number": str(data.get("bank_reference_number") or ""),
            "paid_amount": amount,
            "currency": str(data.get("currency") or "INR").upper(),
            "payment_date": payment_date,
            "remarks": f"Applied from external document {record.document.original_filename}",
        },
    )
    record.applied_payment = payment
    record.applied_at = timezone.now()
    record.reviewed_by = actor
    record.save(update_fields=["applied_payment", "applied_at", "reviewed_by", "updated_at"])
    document = record.document
    only_payment_records = not document.records.exclude(document_type=ExternalDocumentType.PAYMENT_ADVICE).exists()
    all_payments_applied = not document.records.filter(
        applied_payment__isnull=True,
        document_type=ExternalDocumentType.PAYMENT_ADVICE,
    ).exists()
    if only_payment_records and all_payments_applied:
        _set_status(document, ExternalDocumentStatus.APPLIED, actor=actor, event_type="payments_applied")
    _event(document, "payment_applied", actor=actor, metadata={"record_id": record.pk, "invoice_id": record.matched_invoice_id})
    return record


def poll_source(source, *, actor=None, archive=True):
    connector = build_connector(source)
    result = PollResult()
    for remote in connector.list_documents():
        existing = ExternalDocumentImport.objects.filter(source=source, remote_identifier=remote.identifier).first()
        if existing:
            try:
                if archive and existing.status == ExternalDocumentStatus.FAILED:
                    connector.quarantine(remote, existing.last_error)
                elif archive:
                    connector.archive(remote)
            except Exception as exc:
                _event(existing, "source_move_failed", actor=actor, message=str(exc))
                result.errors.append({"identifier": remote.identifier, "error": str(exc)})
            result.documents.append(existing)
            continue
        document = None
        try:
            with connector.open_document(remote) as stream:
                document = register_document(
                    org=source.org,
                    source=source,
                    filename=remote.filename,
                    content=stream.read(),
                    remote_identifier=remote.identifier,
                    remote_path=remote.path,
                    actor=actor,
                )
            if document.status != ExternalDocumentStatus.DUPLICATE:
                document = process_document(document, actor=actor)
            if archive and document.status != ExternalDocumentStatus.FAILED:
                connector.archive(remote)
            elif archive and document.status == ExternalDocumentStatus.FAILED:
                connector.quarantine(remote, document.last_error)
            result.documents.append(document)
        except Exception as exc:
            if document:
                _event(document, "poll_failed", actor=actor, message=str(exc))
            result.errors.append({"identifier": remote.identifier, "error": str(exc)})
    source.last_polled_at = timezone.now()
    source.save(update_fields=["last_polled_at", "updated_at"])
    return result
