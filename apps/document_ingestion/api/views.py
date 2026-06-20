from django.db.models import Count
from django.http import FileResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.access.selectors import get_user_visible_scope_ids
from apps.core.models import ScopeNode
from apps.document_ingestion.api.serializers import (
    ExternalDocumentImportListSerializer,
    ExternalDocumentImportSerializer,
    ExternalDocumentRecordCorrectionSerializer,
    ExternalDocumentRecordLinkSerializer,
    ExternalDocumentRecordSerializer,
    ExternalDocumentSourceSerializer,
    ExternalDocumentUploadSerializer,
)
from apps.document_ingestion.api.permissions import IsDocumentIngestionOperator
from apps.document_ingestion.models import (
    ExternalDocumentImport,
    ExternalDocumentRecord,
    ExternalDocumentSource,
    ExternalDocumentStatus,
    ExternalDocumentType,
    MatchStatus,
)
from apps.document_ingestion.services import (
    IngestionError,
    apply_payment_record,
    correct_record,
    manually_link_record,
    match_document,
    process_document,
    quarantine_document,
    register_document,
)
from apps.invoices.services import PaymentPermissionError, PaymentValidationError


def _visible_org_ids(user):
    if user.is_superuser:
        return None
    scope_ids = get_user_visible_scope_ids(user)
    return ScopeNode.objects.filter(id__in=scope_ids).values_list("org_id", flat=True).distinct()


class ExternalDocumentSourceViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsDocumentIngestionOperator]
    serializer_class = ExternalDocumentSourceSerializer

    def get_queryset(self):
        qs = ExternalDocumentSource.objects.select_related("org").order_by("name")
        org_ids = _visible_org_ids(self.request.user)
        return qs if org_ids is None else qs.filter(org_id__in=org_ids)


class ExternalDocumentImportViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsDocumentIngestionOperator]

    def get_queryset(self):
        qs = (
            ExternalDocumentImport.objects.select_related("org", "source", "created_by", "duplicate_of")
            .prefetch_related(
                "records__matched_vendor", "records__matched_invoice", "records__applied_payment",
                "events__actor",
            )
            .annotate(record_count=Count("records"))
        )
        org_ids = _visible_org_ids(self.request.user)
        if org_ids is not None:
            qs = qs.filter(org_id__in=org_ids)
        params = self.request.query_params
        if value := params.get("org"):
            qs = qs.filter(org_id=value)
        if value := params.get("source"):
            qs = qs.filter(source_id=value)
        if value := params.get("status"):
            qs = qs.filter(status=value)
        if value := params.get("document_type"):
            qs = qs.filter(document_type=value)
        return qs

    def get_serializer_class(self):
        return ExternalDocumentImportListSerializer if self.action == "list" else ExternalDocumentImportSerializer

    @action(detail=False, methods=["get"], url_path="counts")
    def counts(self, request):
        # Status is intentionally excluded so every status tab keeps its global
        # count while org/source/type filters are active.
        qs = ExternalDocumentImport.objects.all()
        org_ids = _visible_org_ids(request.user)
        if org_ids is not None:
            qs = qs.filter(org_id__in=org_ids)
        if value := request.query_params.get("org"):
            qs = qs.filter(org_id=value)
        if value := request.query_params.get("source"):
            qs = qs.filter(source_id=value)
        if value := request.query_params.get("document_type"):
            qs = qs.filter(document_type=value)

        status_counts = {choice: 0 for choice, _ in ExternalDocumentStatus.choices}
        for row in qs.values("status").annotate(total=Count("id")):
            status_counts[row["status"]] = row["total"]

        type_counts = {choice: 0 for choice, _ in ExternalDocumentType.choices}
        for row in qs.values("document_type").annotate(total=Count("id")):
            type_counts[row["document_type"]] = row["total"]

        record_qs = ExternalDocumentRecord.objects.filter(document__in=qs)
        match_counts = {choice: 0 for choice, _ in MatchStatus.choices}
        for row in record_qs.values("match_status").annotate(total=Count("id")):
            match_counts[row["match_status"]] = row["total"]

        return Response({
            "total": qs.count(),
            "by_status": status_counts,
            "by_document_type": type_counts,
            "records_by_match_status": match_counts,
        })

    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, pk=None):
        document = self.get_object()
        if not document.source_file:
            return Response({"detail": "Source document is unavailable."}, status=status.HTTP_404_NOT_FOUND)
        document.source_file.open("rb")
        return FileResponse(
            document.source_file,
            as_attachment=True,
            filename=document.original_filename,
            content_type=document.content_type or "application/octet-stream",
        )

    @action(detail=False, methods=["post"], url_path="upload")
    def upload(self, request):
        serializer = ExternalDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        org = serializer.validated_data["org"]
        org_ids = _visible_org_ids(request.user)
        if org_ids is not None and org.id not in set(org_ids):
            return Response({"detail": "You cannot upload documents for this organization."}, status=status.HTTP_403_FORBIDDEN)
        uploaded = serializer.validated_data["file"]
        document = register_document(
            org=org,
            filename=uploaded.name,
            content=uploaded.read(),
            actor=request.user,
        )
        if document.status != ExternalDocumentStatus.DUPLICATE:
            document = process_document(document, actor=request.user)
        document = self.get_queryset().get(pk=document.pk)
        return Response(ExternalDocumentImportSerializer(document, context={"request": request}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="process")
    def process(self, request, pk=None):
        document = self.get_object()
        try:
            document = process_document(document, actor=request.user, force=bool(request.data.get("force", False)))
        except IngestionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        document = self.get_queryset().get(pk=document.pk)
        return Response(ExternalDocumentImportSerializer(document, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="match")
    def match(self, request, pk=None):
        try:
            document = match_document(self.get_object(), actor=request.user)
        except IngestionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        document = self.get_queryset().get(pk=document.pk)
        return Response(ExternalDocumentImportSerializer(document, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="quarantine")
    def quarantine(self, request, pk=None):
        document = quarantine_document(
            self.get_object(),
            actor=request.user,
            reason=str(request.data.get("reason") or "Manually quarantined."),
        )
        return Response(ExternalDocumentImportSerializer(document, context={"request": request}).data)


class ExternalDocumentRecordViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsDocumentIngestionOperator]
    serializer_class = ExternalDocumentRecordSerializer

    def get_queryset(self):
        qs = ExternalDocumentRecord.objects.select_related(
            "document__org", "matched_vendor", "matched_invoice", "applied_payment", "reviewed_by"
        )
        org_ids = _visible_org_ids(self.request.user)
        if org_ids is not None:
            qs = qs.filter(document__org_id__in=org_ids)
        params = self.request.query_params
        if value := params.get("document"):
            qs = qs.filter(document_id=value)
        if value := params.get("match_status"):
            qs = qs.filter(match_status=value)
        if value := params.get("document_type"):
            qs = qs.filter(document_type=value)
        return qs

    @action(detail=True, methods=["post"], url_path="correct")
    def correct(self, request, pk=None):
        record = self.get_object()
        serializer = ExternalDocumentRecordCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        correct_record(
            record,
            normalized_data=serializer.validated_data["normalized_data"],
            document_type=serializer.validated_data.get("document_type"),
            actor=request.user,
        )
        record.refresh_from_db()
        return Response(self.get_serializer(record).data)

    @action(detail=True, methods=["post"], url_path="link-invoice")
    def link_invoice(self, request, pk=None):
        serializer = ExternalDocumentRecordLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            record = manually_link_record(
                self.get_object(),
                invoice=serializer.validated_data["invoice"],
                actor=request.user,
            )
        except IngestionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(record).data)

    @action(detail=True, methods=["post"], url_path="apply-payment")
    def apply_payment(self, request, pk=None):
        try:
            record = apply_payment_record(self.get_object(), actor=request.user)
        except PaymentPermissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except (IngestionError, PaymentValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(record).data)
