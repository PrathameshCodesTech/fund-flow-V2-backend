from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.viewsets import ModelViewSet
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.invoices.models import Invoice, InvoiceStatus
from apps.invoices.api.serializers import InvoiceSerializer, InvoiceCreateSerializer
from apps.invoices.services import create_invoice, InvoicePermissionError, InvoicePOMandateError
from apps.invoices.selectors import (
    user_can_access_invoice,
    user_can_update_invoice,
    filter_invoices_readable_for_user,
)
from apps.dashboard.services import get_invoice_control_tower_payload


class InvoiceViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "create":
            return InvoiceCreateSerializer
        return InvoiceSerializer

    def get_queryset(self):
        """
        Return only invoices the current user can read:
        - invoices they created, OR
        - invoices at scope nodes where they have READ permission
          at the node or any ancestor.
        """
        qs = Invoice.objects.select_related("scope_node", "created_by").order_by("-created_at")

        node_id = self.request.query_params.get("scope_node")
        invoice_status = self.request.query_params.get("status")
        if node_id:
            qs = qs.filter(scope_node_id=node_id)
        if invoice_status:
            qs = qs.filter(status=invoice_status)

        return filter_invoices_readable_for_user(self.request.user, qs)

    def get_object(self):
        """
        Retrieve a single invoice. Returns 404 if the user has no read access —
        consistent with REST semantics (do not reveal resource existence to
        unauthorized users).
        """
        queryset = Invoice.objects.select_related("scope_node", "created_by")
        node_id = self.request.query_params.get("scope_node")
        invoice_status = self.request.query_params.get("status")
        if node_id:
            queryset = queryset.filter(scope_node_id=node_id)
        if invoice_status:
            queryset = queryset.filter(status=invoice_status)

        # Build the list of accessible invoice IDs for this user
        accessible_ids = set(
            filter_invoices_readable_for_user(self.request.user, queryset).values_list("pk", flat=True)
        )
        # get_object_or_404 uses .get(pk=pk) — bypass for custom filtering
        pk = self.kwargs["pk"]
        if int(pk) not in accessible_ids:
            # Return 404 so unauthorized users cannot enumerate resources
            from django.http import Http404
            raise Http404("Invoice not found.")

        return get_object_or_404(Invoice, pk=pk)

    def update(self, request, *args, **kwargs):
        """
        Partial/full update requires UPDATE permission or creator status.
        Returns 403 if unauthorized.
        """
        invoice = self.get_object()
        if not user_can_update_invoice(request.user, invoice):
            return Response(
                {"detail": "You do not have permission to update this invoice."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = InvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vendor = None
        is_vendor_portal_create = False
        if data.get("vendor"):
            from apps.vendors.models import UserVendorAssignment
            assignment = (
                UserVendorAssignment.objects
                .filter(user=request.user, is_active=True, vendor=data["vendor"])
                .select_related("vendor", "vendor__scope_node")
                .first()
            )
            if not assignment:
                return Response(
                    {"detail": "You are not linked to this vendor."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            vendor = assignment.vendor

            # Verify vendor is active
            from apps.vendors.models import OperationalStatus
            if vendor.operational_status != OperationalStatus.ACTIVE:
                return Response(
                    {"detail": "Your vendor account is not active. Please contact support."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if not (
                data["scope_node"].path == vendor.scope_node.path
                or data["scope_node"].path.startswith(vendor.scope_node.path + "/")
            ):
                return Response(
                    {"detail": "This bill-to entity is outside your vendor scope."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            is_vendor_portal_create = True

        try:
            invoice = create_invoice(
                title=data["title"],
                amount=data["amount"],
                currency=data.get("currency", "INR"),
                scope_node=data["scope_node"],
                created_by=request.user,
                po_number=data.get("po_number", ""),
                vendor=vendor,
                enforce_permission=not is_vendor_portal_create,
            )
        except InvoicePermissionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except InvoicePOMandateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="submit")
    @transaction.atomic
    def submit(self, request, pk=None):
        """
        Submit a draft invoice to pending_workflow status.
        No auto workflow start — workflow is attached explicitly via attach-workflow endpoint.
        """
        invoice = self.get_object()
        if invoice.created_by_id != request.user.pk and not user_can_update_invoice(request.user, invoice):
            return Response(
                {"detail": "You do not have permission to submit this invoice."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.PENDING_WORKFLOW):
            return Response(
                {"detail": f"Invoice cannot be submitted from status '{invoice.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        invoice.status = InvoiceStatus.PENDING_WORKFLOW
        invoice.save(update_fields=["status", "updated_at"])
        invoice.refresh_from_db()
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["get"], url_path="eligible-workflows")
    def eligible_workflows(self, request, pk=None):
        """
        GET /api/v1/invoices/{id}/eligible-workflows/
        Returns published invoice workflow versions visible/allowed for the invoice's scope.
        """
        invoice = self.get_object()
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus
        from apps.core.services import get_ancestors

        # Walk up the scope chain and collect all published invoice workflow versions
        nodes_to_check = [invoice.scope_node] + list(get_ancestors(invoice.scope_node).order_by("-depth"))
        version_ids_seen = set()
        results = []

        for node in nodes_to_check:
            templates = WorkflowTemplate.objects.filter(module="invoice", scope_node=node)
            for template in templates:
                published = (
                    WorkflowTemplateVersion.objects
                    .filter(template=template, status=VersionStatus.PUBLISHED)
                    .order_by("-version_number")
                    .first()
                )
                if published and published.id not in version_ids_seen:
                    version_ids_seen.add(published.id)
                    results.append({
                        "template_id": template.id,
                        "template_name": template.name,
                        "version_id": published.id,
                        "version_number": published.version_number,
                        "scope_node": node.id,
                        "scope_node_name": node.name,
                        "module": "invoice",
                    })

        return Response(results)

    @action(detail=True, methods=["post"], url_path="attach-workflow")
    @transaction.atomic
    def attach_workflow(self, request, pk=None):
        """
        POST /api/v1/invoices/{id}/attach-workflow/
        Explicitly attach a workflow version to an invoice and create a draft instance.

        Request body:
          - template_version_id (required)
          - activate (optional, default False)
        """
        invoice = self.get_object()

        if invoice.status != InvoiceStatus.PENDING_WORKFLOW:
            return Response(
                {"detail": f"Workflow can only be attached to invoices in 'pending_workflow' status. Current: '{invoice.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if invoice.selected_workflow_version_id is not None:
            return Response(
                {"detail": "A workflow version is already attached to this invoice."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        template_version_id = request.data.get("template_version_id")
        if not template_version_id:
            return Response({"detail": "template_version_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        activate = request.data.get("activate", False)

        from apps.workflow.models import WorkflowTemplateVersion, WorkflowInstance, VersionStatus, InstanceStatus
        try:
            template_version = WorkflowTemplateVersion.objects.select_related("template").get(pk=template_version_id)
        except WorkflowTemplateVersion.DoesNotExist:
            return Response({"detail": "Workflow template version not found."}, status=status.HTTP_404_NOT_FOUND)

        if template_version.status != VersionStatus.PUBLISHED:
            return Response({"detail": "Only published workflow versions can be attached."}, status=status.HTTP_400_BAD_REQUEST)

        if template_version.template.module != "invoice":
            return Response({"detail": "This workflow version is not for the invoice module."}, status=status.HTTP_400_BAD_REQUEST)

        # Check no active/non-rejected workflow already attached
        existing = WorkflowInstance.objects.filter(
            subject_type="invoice", subject_id=invoice.pk
        ).exclude(status=InstanceStatus.REJECTED).exists()
        if existing:
            return Response({"detail": "This invoice already has a workflow instance."}, status=status.HTTP_400_BAD_REQUEST)

        # Attach workflow linkage fields
        invoice.selected_workflow_template = template_version.template
        invoice.selected_workflow_version = template_version
        invoice.workflow_selected_by = request.user
        invoice.workflow_selected_at = timezone.now()
        invoice.save(update_fields=[
            "selected_workflow_template", "selected_workflow_version",
            "workflow_selected_by", "workflow_selected_at",
        ])

        # Create draft instance from the explicit version (no resolve_workflow_template_version)
        from apps.workflow.services import create_workflow_instance_draft, activate_workflow_instance
        instance = create_workflow_instance_draft(
            template_version=template_version,
            subject_type="invoice",
            subject_id=invoice.pk,
            subject_scope_node=invoice.scope_node,
            started_by=request.user,
        )

        if activate:
            try:
                activate_workflow_instance(instance, activated_by=request.user)
            except ValueError as e:
                return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        instance.refresh_from_db()
        return Response({
            "invoice": InvoiceSerializer(invoice).data,
            "workflow_instance": {
                "id": instance.id,
                "status": instance.status,
                "template_version_id": instance.template_version_id,
            },
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="allocations")
    def allocations(self, request, pk=None):
        """
        GET /api/v1/invoices/{id}/allocations/
        Returns all InvoiceAllocation rows for this invoice (runtime split context).
        """
        invoice = self.get_object()
        from apps.invoices.models import InvoiceAllocation
        qs = (
            InvoiceAllocation.objects
            .filter(invoice=invoice)
            .select_related(
                "entity", "category", "subcategory", "campaign", "budget",
                "selected_approver", "branch", "split_step__workflow_step",
            )
            .order_by("id")
        )
        data = []
        for a in qs:
            data.append({
                "id": a.id,
                "entity_id": a.entity_id,
                "entity_name": a.entity.name if a.entity else None,
                "category_id": a.category_id,
                "category_name": a.category.name if a.category else None,
                "subcategory_id": a.subcategory_id,
                "subcategory_name": a.subcategory.name if a.subcategory else None,
                "campaign_id": a.campaign_id,
                "campaign_name": a.campaign.name if a.campaign else None,
                "budget_id": a.budget_id,
                "amount": str(a.amount),
                "percentage": str(a.percentage) if a.percentage else None,
                "selected_approver": {
                    "id": a.selected_approver.id,
                    "email": a.selected_approver.email,
                    "first_name": a.selected_approver.first_name,
                    "last_name": a.selected_approver.last_name,
                } if a.selected_approver else None,
                "status": a.status,
                "rejection_reason": a.rejection_reason,
                "note": a.note,
                "branch_id": a.branch_id,
                "branch_status": a.branch.status if a.branch else None,
                "split_step_id": a.split_step_id,
                "split_step_name": a.split_step.workflow_step.name if a.split_step else None,
                "revision_number": a.revision_number,
                "selected_by_id": a.selected_by_id,
                "selected_at": a.selected_at,
                "approved_at": a.approved_at,
                "rejected_at": a.rejected_at,
                "created_at": a.created_at,
            })
        return Response(data)

    @action(detail=True, methods=["get"], url_path="control-tower")
    def control_tower(self, request, pk=None):
        """
        GET /api/v1/invoices/{id}/control-tower/

        Returns a full denormalized payload for the invoice lifecycle control tower:
        - Invoice header summary
        - Selected workflow template/version
        - Current lifecycle phase
        - Current active group/steps
        - All workflow groups + steps
        - Branch summary if any
        - Workflow event timeline
        - Finance handoff summary
        - Blockers/exceptions
        """
        invoice = self.get_object()
        try:
            payload = get_invoice_control_tower_payload(invoice, request.user)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


# ---------------------------------------------------------------------------
# VendorInvoiceSubmission ViewSet
# ---------------------------------------------------------------------------

from rest_framework.viewsets import ViewSet
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from apps.invoices.models import (
    VendorInvoiceSubmission,
    VendorInvoiceSubmissionStatus,
    InvoiceDocument,
)
from apps.invoices.api.serializers import (
    VendorInvoiceSubmissionSerializer,
    VendorInvoiceSubmissionCreateSerializer,
    VendorInvoiceSubmissionUpdateSerializer,
    InvoiceDocumentSerializer,
    InvoiceDocumentCreateSerializer,
)
from apps.invoices.services import (
    create_vendor_invoice_submission,
    extract_invoice_submission,
    update_invoice_submission_fields,
    submit_vendor_invoice_submission,
    SubmissionStateError,
)
from apps.vendors.models import UserVendorAssignment


class VendorInvoiceSubmissionViewSet(ViewSet):
    """
    Endpoints for vendor invoice submissions.

    Vendor users only see their own submissions.
    Internal users can also browse submissions.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _get_vendor_assignment(self):
        """Return the active UserVendorAssignment for the current user, or None."""
        return (
            UserVendorAssignment.objects
            .filter(user=self.request.user, is_active=True)
            .select_related("vendor", "vendor__scope_node")
            .first()
        )

    def list(self, request):
        """
        GET /api/v1/vendor-invoice-submissions/
        Vendor users see only their own; internal users see all.
        """
        assignment = self._get_vendor_assignment()
        qs = VendorInvoiceSubmission.objects.select_related(
            "vendor", "scope_node", "submitted_by", "final_invoice"
        ).order_by("-created_at")

        if assignment:
            qs = qs.filter(vendor=assignment.vendor)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        serializer = VendorInvoiceSubmissionSerializer(qs, many=True)
        return Response(serializer.data)

    def retrieve(self, request, pk=None):
        """
        GET /api/v1/vendor-invoice-submissions/{id}/
        Only the vendor who owns this submission (or an internal user).
        """
        submission = get_object_or_404(
            VendorInvoiceSubmission.objects.select_related(
                "vendor", "scope_node", "submitted_by", "final_invoice"
            ).prefetch_related("documents"),
            pk=pk,
        )
        assignment = self._get_vendor_assignment()
        if assignment and submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Submission not found.")
        serializer = VendorInvoiceSubmissionSerializer(submission)
        return Response(serializer.data)

    @transaction.atomic
    def create(self, request):
        """
        POST /api/v1/vendor-invoice-submissions/
        Vendor uploads an invoice file to start a new submission.
        For manual entry, normalized_data can be sent as a JSON string field.
        """
        assignment = self._get_vendor_assignment()
        if not assignment:
            return Response(
                {"detail": "No active vendor assignment found."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = VendorInvoiceSubmissionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Manual entry: optional pre-filled normalized_data
        normalized_data = None
        nd_raw = request.data.get("normalized_data")
        if nd_raw:
            import json as _json
            try:
                normalized_data = _json.loads(nd_raw) if isinstance(nd_raw, str) else dict(nd_raw)
            except Exception:
                pass

        try:
            submission = create_vendor_invoice_submission(
                user=request.user,
                vendor=assignment.vendor,
                scope_node=serializer.validated_data["scope_node"],
                file_obj=serializer.validated_data["source_file"],
                normalized_data=normalized_data,
            )
        except SubmissionStateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            VendorInvoiceSubmissionSerializer(submission).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path="template/excel", permission_classes=[AllowAny])
    def template_excel(self, request):
        """GET /api/v1/vendor-invoice-submissions/template/excel/ — download .xlsx template."""
        from apps.invoices.services import generate_excel_template
        return generate_excel_template()

    @action(detail=False, methods=["get"], url_path="template/pdf", permission_classes=[AllowAny])
    def template_pdf(self, request):
        """GET /api/v1/vendor-invoice-submissions/template/pdf/ — download PDF template."""
        from apps.invoices.services import generate_pdf_template
        return generate_pdf_template()

    @action(detail=True, methods=["post"])
    def extract(self, request, pk=None):
        """
        POST /api/v1/vendor-invoice-submissions/{id}/extract/
        Re-run extraction on the source file.
        """
        submission = get_object_or_404(
            VendorInvoiceSubmission.objects.select_related("vendor"),
            pk=pk,
        )
        assignment = self._get_vendor_assignment()
        if assignment and submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Submission not found.")

        try:
            result = extract_invoice_submission(submission)
        except SubmissionStateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        submission.refresh_from_db()
        return Response({
            "status": submission.status,
            "confidence": result.confidence,
            "confidence_percent": round(result.confidence * 100, 1),
            "normalized_data": submission.normalized_data,
            "validation_errors": submission.validation_errors,
            "warnings": result.warnings,
            "errors": result.errors,
        })

    def partial_update(self, request, pk=None):
        """
        PATCH /api/v1/vendor-invoice-submissions/{id}/
        Vendor corrects normalised fields from extraction.
        """
        submission = get_object_or_404(
            VendorInvoiceSubmission.objects.select_related("vendor"),
            pk=pk,
        )
        assignment = self._get_vendor_assignment()
        if assignment and submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Submission not found.")

        serializer = VendorInvoiceSubmissionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        submission = update_invoice_submission_fields(
            submission,
            serializer.validated_data["normalized_data"],
        )
        return Response(VendorInvoiceSubmissionSerializer(submission).data)

    def update(self, request, pk=None):
        return self.partial_update(request, pk=pk)

    @transaction.atomic
    @action(detail=True, methods=["post"], url_path="submit")
    def submit_invoice(self, request, pk=None):
        """
        POST /api/v1/vendor-invoice-submissions/{id}/submit/
        Finalize submission: validate, create Invoice, start workflow.
        """
        submission = get_object_or_404(
            VendorInvoiceSubmission.objects.select_related(
                "vendor", "scope_node"
            ),
            pk=pk,
        )
        assignment = self._get_vendor_assignment()
        if assignment and submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Submission not found.")

        try:
            invoice = submit_vendor_invoice_submission(submission, user=request.user)
        except SubmissionStateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "detail": "Invoice created and submitted for review.",
            "invoice_id": invoice.pk,
            "submission_status": submission.status,
        })

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel_submission(self, request, pk=None):
        """
        POST /api/v1/vendor-invoice-submissions/{id}/cancel/
        Cancel a submission. Only owner vendor, only from allowed states.
        """
        submission = get_object_or_404(
            VendorInvoiceSubmission.objects.select_related(
                "vendor", "scope_node"
            ),
            pk=pk,
        )
        assignment = self._get_vendor_assignment()
        if assignment and submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Submission not found.")

        from apps.invoices.services import cancel_vendor_invoice_submission, SubmissionStateError
        try:
            cancel_vendor_invoice_submission(submission)
        except SubmissionStateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(VendorInvoiceSubmissionSerializer(submission).data)

    @action(detail=True, methods=["post"], url_path="documents")
    def add_document(self, request, pk=None):
        """
        POST /api/v1/vendor-invoice-submissions/{id}/documents/
        Attach a supporting document to this submission.
        """
        submission = get_object_or_404(
            VendorInvoiceSubmission.objects.select_related("vendor"),
            pk=pk,
        )
        assignment = self._get_vendor_assignment()
        if assignment and submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Submission not found.")

        serializer = InvoiceDocumentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        file_obj = serializer.validated_data["file"]
        file_name = file_obj.name
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

        doc = InvoiceDocument.objects.create(
            submission=submission,
            file=file_obj,
            file_name=file_name,
            file_type=ext,
            document_type=serializer.validated_data["document_type"],
            uploaded_by=request.user,
        )
        return Response(
            InvoiceDocumentSerializer(doc, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# InvoiceDocument ViewSet (standalone download)
# ---------------------------------------------------------------------------

class InvoiceDocumentViewSet(ViewSet):
    permission_classes = [IsAuthenticated]

    def retrieve(self, request, pk=None):
        doc = get_object_or_404(
            InvoiceDocument.objects.select_related("invoice", "submission"),
            pk=pk,
        )
        # Only allow owner vendor or internal users
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .first()
        )
        if assignment and doc.submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Document not found.")
        if not doc.file:
            return Response({"detail": "No file attached."}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "id": doc.id,
            "file_name": doc.file_name,
            "document_type": doc.document_type,
            "download_url": request.build_absolute_uri(doc.file.url),
        })
