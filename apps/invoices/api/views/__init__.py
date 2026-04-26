from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.viewsets import ModelViewSet
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from apps.core.models import ScopeNode

from apps.invoices.models import Invoice, InvoiceStatus, InvoicePayment, PaymentMethod
from apps.invoices.api.serializers import (
    InvoiceSerializer, InvoiceCreateSerializer,
    InvoicePaymentSerializer, VendorInvoicePaymentSerializer, InvoicePaymentUpdateSerializer,
)
from apps.invoices.services import (
    create_invoice, InvoicePermissionError, InvoicePOMandateError,
    record_invoice_payment, PaymentPermissionError, PaymentValidationError,
)
from apps.invoices.selectors import (
    user_can_access_invoice,
    user_can_update_invoice,
    filter_invoices_readable_for_user,
    get_invoice_eligible_workflow_routes,
    user_can_begin_invoice_review,
    user_can_record_invoice_payment,
)
from apps.dashboard.services import get_invoice_control_tower_payload


class InvoiceViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]

    def _apply_scope_filter(self, qs, node_id: str | None):
        if not node_id:
            return qs
        try:
            node = ScopeNode.objects.get(pk=node_id)
        except ScopeNode.DoesNotExist:
            return qs.none()
        return qs.filter(scope_node__path__startswith=node.path)

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
        qs = self._apply_scope_filter(qs, node_id)
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
        queryset = self._apply_scope_filter(queryset, node_id)
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

    @action(detail=True, methods=["get"], url_path="payment")
    def get_payment(self, request, pk=None):
        """
        GET /api/v1/invoices/{id}/payment/
        Return the payment record for this invoice (or 404 if none exists).
        """
        invoice = self.get_object()
        try:
            payment = invoice.payment_record
        except InvoicePayment.DoesNotExist:
            return Response(
                {"detail": "No payment record found for this invoice."},
                status=status.HTTP_404_NOT_FOUND,
            )
        from apps.vendors.models import UserVendorAssignment
        is_vendor_user = UserVendorAssignment.objects.filter(
            user=request.user,
            vendor=invoice.vendor,
            is_active=True,
        ).exists() if invoice.vendor_id else False

        serializer_cls = VendorInvoicePaymentSerializer if is_vendor_user else InvoicePaymentSerializer
        serializer = serializer_cls(payment, context={"request": request})
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, pk=None):
        """
        POST /api/v1/invoices/{id}/record-payment/
        Create or update the payment record for this invoice.

        Permission: only workflow participants, invoice creator, or admin.
        Validation: when marking PAID, requires payment_method, payment_date,
        paid_amount > 0, and at least one of payment_reference_number / utr_number.
        """
        invoice = self.get_object()

        # Permission check first
        if not user_can_record_invoice_payment(request.user, invoice):
            return Response(
                {"detail": "You do not have permission to record payment for this invoice."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = InvoicePaymentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            payment = record_invoice_payment(
                invoice=invoice,
                actor=request.user,
                data=serializer.validated_data,
            )
        except PaymentPermissionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except PaymentValidationError as e:
            if isinstance(e.args[0], dict):
                return Response(e.args[0], status=status.HTTP_400_BAD_REQUEST)
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(InvoicePaymentSerializer(payment, context={"request": request}).data)

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
            # Only active templates are eligible
            templates = WorkflowTemplate.objects.filter(
                module="invoice", scope_node=node, is_active=True
            )
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
                        "template_code": template.code,
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

        Authorization: same access rules as invoice read (creator or READ permission).
        Race protection: uses select_for_update(nowait=True) to prevent two concurrent
        callers from both attaching a workflow to the same invoice.
        """
        from django.http import Http404

        # First: verify the caller can access this invoice at all (creator or READ).
        invoice = self.get_object()

        # Second: acquire a row lock on the invoice to serialize concurrent attach attempts.
        try:
            invoice = Invoice.objects.select_for_update(nowait=True).get(pk=pk)
        except Invoice.DoesNotExist:
            raise Http404("Invoice not found.")
        except Exception:
            return Response(
                {"detail": "Invoice is currently being processed. Please try again."},
                status=status.HTTP_409_CONFLICT,
            )

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

        if not template_version.template.is_active:
            return Response({"detail": "The selected workflow template is not active."}, status=status.HTTP_400_BAD_REQUEST)

        # Template scope must be the invoice's scope node or one of its ancestors.
        from apps.core.services import get_ancestors
        valid_scope_ids = {invoice.scope_node_id} | set(
            get_ancestors(invoice.scope_node).values_list("id", flat=True)
        )
        if template_version.template.scope_node_id not in valid_scope_ids:
            return Response(
                {"detail": "The selected workflow template is not configured for this invoice's scope or any of its ancestors."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check no active/non-rejected workflow already attached (re-check under lock)
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

    @action(detail=False, methods=["get"], url_path="pending-review")
    def pending_review(self, request):
        """
        GET /api/v1/invoices/pending-review/

        Returns pending_workflow invoices with available workflow routes.

        Route visibility rule:
          - User with START_WORKFLOW:INVOICE → sees ALL active published routes.
          - User without START_WORKFLOW → sees only routes where they are
            first-step eligible (user_can_begin == True).
          - Invoice row is included only if it has at least one visible route.

        This prevents a first-step approver from seeing routes they cannot start.
        """
        from apps.access.models import PermissionAction, PermissionResource
        from apps.access.services import user_has_permission_including_ancestors
        from apps.core.services import get_ancestors
        from apps.workflow.models import WorkflowInstance, InstanceStatus

        qs = (
            Invoice.objects
            .filter(status=InvoiceStatus.PENDING_WORKFLOW, selected_workflow_version__isnull=True)
            .exclude(
                pk__in=WorkflowInstance.objects.filter(subject_type="invoice")
                .exclude(status=InstanceStatus.REJECTED)
                .values_list("subject_id", flat=True)
            )
            .select_related("scope_node", "created_by", "vendor")
            .order_by("-created_at")
        )

        results = []
        for invoice in qs:
            # Check START_WORKFLOW at the invoice's scope node or ancestors.
            # If granted, user sees ALL routes. Otherwise only first-step-eligible routes.
            invoice_and_ancestors = [invoice.scope_node] + list(get_ancestors(invoice.scope_node))
            user_has_sw = any(
                user_has_permission_including_ancestors(
                    request.user,
                    PermissionAction.START_WORKFLOW,
                    PermissionResource.INVOICE,
                    node,
                )
                for node in invoice_and_ancestors
            )

            all_routes = get_invoice_eligible_workflow_routes(invoice, user=request.user)

            # Apply route-level visibility filter
            visible_routes = all_routes if user_has_sw else [r for r in all_routes if r["user_can_begin"]]
            if not visible_routes:
                continue

            results.append({
                "id": invoice.id,
                "title": invoice.title,
                "amount": str(invoice.amount),
                "currency": invoice.currency,
                "vendor_name": invoice.vendor.vendor_name if invoice.vendor_id else None,
                "scope_node": invoice.scope_node_id,
                "scope_node_name": invoice.scope_node.name if invoice.scope_node else None,
                "created_at": invoice.created_at,
                "available_routes": visible_routes,
            })

        return Response(results)

    @action(detail=True, methods=["post"], url_path="begin-review")
    @transaction.atomic
    def begin_review(self, request, pk=None):
        """
        POST /api/v1/invoices/{id}/begin-review/
        Body: { template_version_id }

        Wraps attach + draft creation + optional self-assignment + activation
        inside a single locked transaction. Returns:
            { "status": "activated", "invoice_id", "workflow_instance_id" }
        or:
            { "status": "assignment_required", "invoice_id", "workflow_instance_id", "detail" }
        """
        # Lock the row to prevent two concurrent begin-review calls on the same invoice.
        try:
            invoice = Invoice.objects.select_for_update(nowait=True).get(pk=pk)
        except Invoice.DoesNotExist:
            from django.http import Http404
            raise Http404("Invoice not found.")
        except Exception:
            return Response(
                {"detail": "Invoice is currently being processed. Please try again."},
                status=status.HTTP_409_CONFLICT,
            )

        # Authorization: must be able to begin review (not general READ check).
        # We validate against the selected version later; for now just verify
        # the invoice exists and user has some organizational visibility.
        if not invoice.scope_node_id:
            return Response({"detail": "Invoice has no scope node."}, status=status.HTTP_400_BAD_REQUEST)

        # Validate invoice state before touching anything.
        if invoice.status != InvoiceStatus.PENDING_WORKFLOW:
            return Response(
                {"detail": f"Invoice is not in pending_workflow status. Current: '{invoice.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if invoice.selected_workflow_version_id is not None:
            return Response(
                {"detail": "A workflow version is already attached to this invoice."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.workflow.models import WorkflowTemplateVersion, WorkflowInstance, VersionStatus, InstanceStatus

        existing = WorkflowInstance.objects.filter(
            subject_type="invoice", subject_id=invoice.pk
        ).exclude(status=InstanceStatus.REJECTED).exists()
        if existing:
            return Response(
                {"detail": "This invoice already has a workflow instance."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        template_version_id = request.data.get("template_version_id")
        if not template_version_id:
            return Response({"detail": "template_version_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            template_version = WorkflowTemplateVersion.objects.select_related("template").get(pk=template_version_id)
        except WorkflowTemplateVersion.DoesNotExist:
            return Response({"detail": "Workflow template version not found."}, status=status.HTTP_404_NOT_FOUND)

        # Validate the selected version is usable.
        if template_version.status != VersionStatus.PUBLISHED:
            return Response({"detail": "Only published workflow versions can be used."}, status=status.HTTP_400_BAD_REQUEST)

        if template_version.template.module != "invoice":
            return Response({"detail": "This workflow version is not for the invoice module."}, status=status.HTTP_400_BAD_REQUEST)

        if not template_version.template.is_active:
            return Response({"detail": "The selected workflow template is not active."}, status=status.HTTP_400_BAD_REQUEST)

        from apps.core.services import get_ancestors
        valid_scope_ids = {invoice.scope_node_id} | set(
            get_ancestors(invoice.scope_node).values_list("id", flat=True)
        )
        if template_version.template.scope_node_id not in valid_scope_ids:
            return Response(
                {"detail": "The selected workflow template is not configured for this invoice's scope or any of its ancestors."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Permission gate: admin role OR first-step eligible for this specific route.
        if not user_can_begin_invoice_review(request.user, invoice, template_version):
            return Response(
                {"detail": "You are not eligible to begin review on this invoice with the selected workflow."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Attach the workflow version to the invoice.
        invoice.selected_workflow_template = template_version.template
        invoice.selected_workflow_version = template_version
        invoice.workflow_selected_by = request.user
        invoice.workflow_selected_at = timezone.now()
        invoice.save(update_fields=[
            "selected_workflow_template", "selected_workflow_version",
            "workflow_selected_by", "workflow_selected_at",
        ])

        # Create a draft workflow instance.
        from apps.workflow.services import (
            create_workflow_instance_draft,
            activate_workflow_instance,
            get_first_actionable_step,
            get_eligible_users_for_step,
        )
        instance = create_workflow_instance_draft(
            template_version=template_version,
            subject_type="invoice",
            subject_id=invoice.pk,
            subject_scope_node=invoice.scope_node,
            started_by=request.user,
        )

        # If the actor is eligible for the first human step, assign them to it
        # (only if the step has no assignee yet — create_workflow_instance_draft
        # may have already resolved default_user or single-eligible auto-assign).
        first_step = get_first_actionable_step(template_version)
        if first_step:
            eligible = get_eligible_users_for_step(first_step, invoice.scope_node)
            if eligible.filter(pk=request.user.pk).exists():
                from apps.workflow.models import WorkflowInstanceStep, AssignmentState
                ist = WorkflowInstanceStep.objects.filter(
                    instance_group__instance=instance,
                    workflow_step=first_step,
                ).first()
                if ist and ist.assigned_user_id is None:
                    ist.assigned_user = request.user
                    ist.assignment_state = AssignmentState.ASSIGNED
                    ist.save(update_fields=["assigned_user", "assignment_state"])

        # Attempt activation. If unassigned steps remain, keep the draft and
        # tell the frontend to redirect to the assignment page.
        try:
            activate_workflow_instance(instance, activated_by=request.user)
            return Response({
                "status": "activated",
                "invoice_id": invoice.id,
                "workflow_instance_id": instance.id,
            })
        except ValueError:
            return Response({
                "status": "assignment_required",
                "invoice_id": invoice.id,
                "workflow_instance_id": instance.id,
                "detail": "Workflow attached but some reviewers must be assigned before activation.",
            })

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
    submit_vendor_invoice_with_route,
    SubmissionStateError,
    SubmissionValidationError,
    VendorRouteError,
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
            "vendor", "scope_node", "submitted_by", "final_invoice", "correction_requested_by"
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
                "vendor", "scope_node", "submitted_by", "final_invoice", "correction_requested_by"
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

        Required body field:
          send_to_option_id — ID of an active VendorSubmissionRoute for this org.

        Creates the Invoice, attaches the workflow version resolved from the
        route's mapped template, and activates the workflow — all in one atomic
        transaction.  If workflow activation fails (unresolved assignees /
        misconfiguration) the entire transaction is rolled back and a 400 is
        returned with a clear explanation.
        """
        submission = get_object_or_404(
            VendorInvoiceSubmission.objects.select_related(
                "vendor", "vendor__org", "scope_node"
            ),
            pk=pk,
        )
        assignment = self._get_vendor_assignment()
        if assignment and submission.vendor_id != assignment.vendor_id:
            from django.http import Http404
            raise Http404("Submission not found.")

        from apps.invoices.api.serializers import VendorInvoiceSubmissionSubmitSerializer
        serializer = VendorInvoiceSubmissionSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        send_to_option_id = serializer.validated_data["send_to_option_id"]

        # Resolve route — must exist, be active, and belong to vendor's org
        from apps.vendors.models import VendorSubmissionRoute
        try:
            send_to_route = VendorSubmissionRoute.objects.select_related(
                "workflow_template"
            ).get(pk=send_to_option_id, org=submission.vendor.org)
        except VendorSubmissionRoute.DoesNotExist:
            return Response(
                {"detail": "Invalid 'Send To' option — not found for this vendor's org."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not send_to_route.is_active:
            return Response(
                {"detail": f"'Send To' option '{send_to_route.label}' is not active."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            invoice, warnings = submit_vendor_invoice_with_route(
                submission,
                user=request.user,
                send_to_route=send_to_route,
            )
        except SubmissionValidationError as e:
            return Response({
                "detail": "Submission validation failed.",
                "errors": e.result.field_errors,
                "warnings": e.result.warnings,
            }, status=status.HTTP_400_BAD_REQUEST)
        except SubmissionStateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except VendorRouteError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        response_data = {
            "detail": "Invoice created and routed for review.",
            "invoice_id": invoice.pk,
            "invoice_status": invoice.status,
            "submission_status": submission.status,
        }
        if warnings:
            response_data["warnings"] = warnings
        return Response(response_data)

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
