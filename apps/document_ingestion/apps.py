from django.apps import AppConfig


class DocumentIngestionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.document_ingestion"
    verbose_name = "External Document Ingestion"

