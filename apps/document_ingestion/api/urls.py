from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.document_ingestion.api.views import (
    ExternalDocumentImportViewSet,
    ExternalDocumentRecordViewSet,
    ExternalDocumentSourceViewSet,
)

router = DefaultRouter()
router.register("sources", ExternalDocumentSourceViewSet, basename="external-document-source")
router.register("documents", ExternalDocumentImportViewSet, basename="external-document")
router.register("records", ExternalDocumentRecordViewSet, basename="external-document-record")

urlpatterns = [path("", include(router.urls))]

