from rest_framework import serializers

from apps.core.models import Organization
from apps.invoices.models import Invoice
from apps.document_ingestion.models import (
    ExternalDocumentEvent,
    ExternalDocumentImport,
    ExternalDocumentRecord,
    ExternalDocumentSource,
)


class ExternalDocumentEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = ExternalDocumentEvent
        fields = (
            "id", "event_type", "from_status", "to_status", "message",
            "metadata", "actor", "actor_email", "created_at",
        )


class ExternalDocumentRecordSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source="matched_vendor.vendor_name", read_only=True, allow_null=True)
    invoice_title = serializers.CharField(source="matched_invoice.title", read_only=True, allow_null=True)

    class Meta:
        model = ExternalDocumentRecord
        fields = (
            "id", "record_index", "document_type", "match_status", "raw_data",
            "normalized_data", "confidence_score", "validation_errors",
            "matched_vendor", "vendor_name", "matched_invoice", "invoice_title",
            "applied_payment", "matched_at", "applied_at", "reviewed_by",
            "created_at", "updated_at",
        )
        read_only_fields = fields


class ExternalDocumentImportSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name", read_only=True, allow_null=True)
    org_name = serializers.CharField(source="org.name", read_only=True)
    records = ExternalDocumentRecordSerializer(many=True, read_only=True)
    events = ExternalDocumentEventSerializer(many=True, read_only=True)
    download_url = serializers.SerializerMethodField()

    def get_download_url(self, obj):
        return f"/api/v1/document-ingestion/documents/{obj.pk}/download/"

    class Meta:
        model = ExternalDocumentImport
        fields = (
            "id", "org", "org_name", "source", "source_name",
            "original_filename", "content_type", "file_size", "content_hash",
            "download_url", "document_type", "status", "raw_extracted_data", "normalized_data",
            "validation_errors", "duplicate_of", "processing_attempts", "last_error",
            "discovered_at", "downloaded_at", "extracted_at", "created_by",
            "created_at", "updated_at", "records", "events",
        )
        read_only_fields = fields


class ExternalDocumentImportListSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name", read_only=True, allow_null=True)
    org_name = serializers.CharField(source="org.name", read_only=True)
    record_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ExternalDocumentImport
        fields = (
            "id", "org", "org_name", "source", "source_name", "original_filename",
            "content_type", "file_size", "content_hash", "document_type", "status",
            "record_count", "processing_attempts", "last_error", "created_at", "updated_at",
        )


class ExternalDocumentSourceSerializer(serializers.ModelSerializer):
    org_name = serializers.CharField(source="org.name", read_only=True)

    class Meta:
        model = ExternalDocumentSource
        fields = (
            "id", "org", "org_name", "name", "connector_type",
            "is_active", "last_polled_at", "created_at", "updated_at",
        )
        read_only_fields = fields


class ExternalDocumentUploadSerializer(serializers.Serializer):
    org = serializers.PrimaryKeyRelatedField(queryset=Organization.objects.filter(is_active=True))
    file = serializers.FileField()

    def validate_file(self, value):
        allowed = {".pdf", ".xlsx", ".xlsm", ".csv", ".json", ".txt", ".text"}
        from pathlib import Path
        if Path(value.name).suffix.lower() not in allowed:
            raise serializers.ValidationError(f"Supported file types: {', '.join(sorted(allowed))}.")
        from django.conf import settings
        max_bytes = getattr(settings, "DOCUMENT_INGESTION_MAX_FILE_SIZE_MB", 25) * 1024 * 1024
        if value.size > max_bytes:
            raise serializers.ValidationError(f"File exceeds the {max_bytes // (1024 * 1024)} MB size limit.")
        return value


class ExternalDocumentRecordCorrectionSerializer(serializers.Serializer):
    normalized_data = serializers.JSONField()
    document_type = serializers.ChoiceField(
        choices=("unknown", "invoice", "payment_advice"), required=False
    )


class ExternalDocumentRecordLinkSerializer(serializers.Serializer):
    invoice = serializers.PrimaryKeyRelatedField(queryset=Invoice.objects.select_related("scope_node", "vendor"))
