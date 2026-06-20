from django.contrib import admin

from apps.document_ingestion.models import (
    ExternalDocumentEvent,
    ExternalDocumentImport,
    ExternalDocumentRecord,
    ExternalDocumentSource,
)


@admin.register(ExternalDocumentSource)
class ExternalDocumentSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "org", "connector_type", "is_active", "last_polled_at")
    list_filter = ("connector_type", "is_active", "org")
    search_fields = ("name", "config_key", "base_path")


@admin.register(ExternalDocumentImport)
class ExternalDocumentImportAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "document_type", "status", "org")
    list_filter = ("status", "document_type", "org")
    search_fields = ("original_filename", "content_hash", "remote_identifier")
    readonly_fields = ("content_hash", "raw_extracted_data", "normalized_data", "validation_errors")


@admin.register(ExternalDocumentRecord)
class ExternalDocumentRecordAdmin(admin.ModelAdmin):
    list_display = ("document", "record_index", "document_type", "match_status", "matched_invoice", "applied_payment")
    list_filter = ("document_type", "match_status")
    search_fields = ("document__original_filename",)


@admin.register(ExternalDocumentEvent)
class ExternalDocumentEventAdmin(admin.ModelAdmin):
    list_display = ("document", "event_type", "from_status", "to_status", "created_at")
    list_filter = ("event_type", "to_status")
    readonly_fields = ("document", "event_type", "from_status", "to_status", "message", "metadata", "actor", "created_at")
