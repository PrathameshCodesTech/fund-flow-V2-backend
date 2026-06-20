from django.conf import settings
from django.db import models


class ConnectorType(models.TextChoices):
    LOCAL = "local", "Local Folder"
    SFTP = "sftp", "SFTP"
    FTP = "ftp", "FTP"
    SHAREPOINT = "sharepoint", "SharePoint"


class ExternalDocumentType(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    INVOICE = "invoice", "Invoice"
    PAYMENT_ADVICE = "payment_advice", "Payment Advice"


class ExternalDocumentStatus(models.TextChoices):
    DISCOVERED = "discovered", "Discovered"
    DOWNLOADED = "downloaded", "Downloaded"
    EXTRACTED = "extracted", "Extracted"
    MATCHED = "matched", "Matched"
    REVIEW_REQUIRED = "review_required", "Review Required"
    APPLIED = "applied", "Applied"
    DUPLICATE = "duplicate", "Duplicate"
    QUARANTINED = "quarantined", "Quarantined"
    FAILED = "failed", "Failed"


class MatchStatus(models.TextChoices):
    NOT_ATTEMPTED = "not_attempted", "Not Attempted"
    MATCHED = "matched", "Matched"
    AMBIGUOUS = "ambiguous", "Ambiguous"
    UNMATCHED = "unmatched", "Unmatched"
    CONFLICT = "conflict", "Conflict"


class ExternalDocumentSource(models.Model):
    """Provider configuration without credentials.

    Secrets are resolved from environment variables using ``config_key`` and
    are never persisted in this table.
    """

    org = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="document_sources")
    name = models.CharField(max_length=255)
    connector_type = models.CharField(max_length=30, choices=ConnectorType.choices)
    config_key = models.CharField(
        max_length=100,
        help_text="Environment-variable prefix used by the connector, e.g. HORIZON_FINANCE_SFTP.",
    )
    base_path = models.CharField(max_length=1000, blank=True)
    public_config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "external_document_sources"
        constraints = [
            models.UniqueConstraint(fields=["org", "name"], name="unique_document_source_name_per_org"),
        ]
        indexes = [models.Index(fields=["org", "is_active"])]

    def __str__(self):
        return f"{self.name} ({self.connector_type})"


def external_document_upload_path(instance, filename):
    org_id = instance.org_id or "unknown"
    return f"external_document_imports/org_{org_id}/{filename}"


class ExternalDocumentImport(models.Model):
    org = models.ForeignKey("core.Organization", on_delete=models.PROTECT, related_name="external_document_imports")
    source = models.ForeignKey(
        ExternalDocumentSource,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="documents",
    )
    remote_identifier = models.CharField(max_length=1000, blank=True)
    remote_path = models.CharField(max_length=2000, blank=True)
    original_filename = models.CharField(max_length=500)
    content_type = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    content_hash = models.CharField(max_length=64, db_index=True)
    source_file = models.FileField(upload_to=external_document_upload_path)
    document_type = models.CharField(
        max_length=30,
        choices=ExternalDocumentType.choices,
        default=ExternalDocumentType.UNKNOWN,
    )
    status = models.CharField(
        max_length=30,
        choices=ExternalDocumentStatus.choices,
        default=ExternalDocumentStatus.DISCOVERED,
        db_index=True,
    )
    raw_extracted_data = models.JSONField(default=dict, blank=True)
    normalized_data = models.JSONField(default=dict, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicate_documents"
    )
    processing_attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    discovered_at = models.DateTimeField(auto_now_add=True)
    downloaded_at = models.DateTimeField(null=True, blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_external_document_imports",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "external_document_imports"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "remote_identifier"],
                condition=~models.Q(remote_identifier=""),
                name="unique_remote_document_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "status"]),
            models.Index(fields=["document_type", "status"]),
        ]

    def __str__(self):
        return f"ExternalDocumentImport {self.pk}: {self.original_filename} [{self.status}]"


class ExternalDocumentRecord(models.Model):
    """One business record extracted from a physical file."""

    document = models.ForeignKey(ExternalDocumentImport, on_delete=models.CASCADE, related_name="records")
    record_index = models.PositiveIntegerField(default=1)
    document_type = models.CharField(
        max_length=30,
        choices=ExternalDocumentType.choices,
        default=ExternalDocumentType.UNKNOWN,
    )
    match_status = models.CharField(
        max_length=30,
        choices=MatchStatus.choices,
        default=MatchStatus.NOT_ATTEMPTED,
        db_index=True,
    )
    raw_data = models.JSONField(default=dict, blank=True)
    normalized_data = models.JSONField(default=dict, blank=True)
    confidence_score = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    matched_vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.SET_NULL, null=True, blank=True, related_name="matched_external_document_records"
    )
    matched_invoice = models.ForeignKey(
        "invoices.Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="matched_external_document_records"
    )
    applied_payment = models.ForeignKey(
        "invoices.InvoicePayment", on_delete=models.SET_NULL, null=True, blank=True, related_name="source_document_records"
    )
    matched_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_external_document_records",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "external_document_records"
        ordering = ["record_index"]
        constraints = [
            models.UniqueConstraint(fields=["document", "record_index"], name="unique_record_index_per_document"),
        ]
        indexes = [
            models.Index(fields=["document_type", "match_status"]),
            models.Index(fields=["matched_invoice"]),
        ]

    def __str__(self):
        return f"ExternalDocumentRecord {self.document_id}:{self.record_index} [{self.match_status}]"


class ExternalDocumentEvent(models.Model):
    document = models.ForeignKey(ExternalDocumentImport, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=100)
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30, blank=True)
    message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="external_document_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "external_document_events"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["document", "created_at"])]
