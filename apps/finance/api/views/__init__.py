"""
Finance API — internal + public endpoints.

Internal (authenticated):
    GET  /api/v1/finance/handoffs/           — list handoffs (filterable)
    GET  /api/v1/finance/handoffs/{id}/      — handoff detail

Public (no auth):
    GET  /api/v1/finance/public/{token}/    — token metadata
    POST /api/v1/finance/public/{token}/approve/  — finance approve
    POST /api/v1/finance/public/{token}/reject/   — finance reject
"""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.selectors import get_user_visible_scope_ids
from apps.finance.models import FinanceActionToken, FinanceDecision, FinanceHandoff
from apps.finance.services import (
    TokenError,
    HandoffStateError,
    FinanceHandoffError,
    NoFinanceRecipientsError,
    approve_finance_handoff,
    create_finance_handoff,
    finance_approve_handoff,
    finance_reject_handoff,
    get_active_handoff_for_subject,
    get_handoff_by_token,
    reject_finance_handoff,
    resolve_finance_recipients_for_handoff,
    send_finance_handoff,
)
from apps.finance.api.serializers import (
    FinanceApproveSerializer,
    FinanceDecisionSerializer,
    FinanceHandoffSerializer,
    FinanceRejectSerializer,
    PublicFinanceTokenSerializer,
    InvoiceFinanceReviewSerializer,
)


# ---------------------------------------------------------------------------
# Internal: FinanceHandoff list/detail
# ---------------------------------------------------------------------------

class FinanceHandoffViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Authenticated list/detail for FinanceHandoffs.
    Filters: module, subject_type, subject_id, status, org, scope_node
    """
    permission_classes = [IsAuthenticated]
    serializer_class = FinanceHandoffSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = FinanceHandoff.objects.select_related(
            "org", "scope_node", "submitted_by"
        ).filter(scope_node_id__in=visible_scope_ids).order_by("-created_at")

        params = self.request.query_params
        if module := params.get("module"):
            qs = qs.filter(module=module)
        if subject_type := params.get("subject_type"):
            qs = qs.filter(subject_type=subject_type)
        if subject_id := params.get("subject_id"):
            qs = qs.filter(subject_id=subject_id)
        if handoff_status := params.get("status"):
            qs = qs.filter(status=handoff_status)
        if org_id := params.get("org"):
            qs = qs.filter(org_id=org_id)
        if scope_node_id := params.get("scope_node"):
            qs = qs.filter(scope_node_id=scope_node_id)
        return qs

    @action(detail=True, methods=["post"], url_path="send")
    def send(self, request, pk=None):
        """POST /api/v1/finance/handoffs/{id}/send/ — resend the handoff email."""
        handoff = self.get_object()
        try:
            updated = send_finance_handoff(handoff, triggered_by=request.user)
        except FinanceHandoffError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(FinanceHandoffSerializer(updated).data)

    def _can_current_user_act_as_finance(self, handoff: FinanceHandoff) -> bool:
        user = self.request.user
        if user.is_superuser:
            return True
        email = (getattr(user, "email", "") or "").strip().lower()
        if email:
            try:
                recipients = resolve_finance_recipients_for_handoff(handoff)
            except NoFinanceRecipientsError:
                recipients = []
            if email in {r.strip().lower() for r in recipients if r}:
                return True
        return self._user_has_finance_role_for_handoff(handoff)

    def _user_has_finance_role_for_handoff(self, handoff: FinanceHandoff) -> bool:
        from django.conf import settings

        from apps.access.models import UserRoleAssignment

        scope_node = handoff.scope_node
        if not scope_node:
            return False

        scope_ids = []
        current = scope_node
        while current:
            if current.id not in scope_ids:
                scope_ids.append(current.id)
            current = getattr(current, "parent", None)

        role_codes = set(getattr(settings, "FINANCE_ROLE_CODES", {"finance_team"}))
        return UserRoleAssignment.objects.filter(
            user=self.request.user,
            role__code__in=role_codes,
            role__is_active=True,
            scope_node_id__in=scope_ids,
        ).exists()

    def _finance_permission_denied(self):
        return Response(
            {"detail": "You are not a finance recipient for this handoff."},
            status=status.HTTP_403_FORBIDDEN,
        )

    @action(detail=True, methods=["get"], url_path="review")
    def review(self, request, pk=None):
        """
        GET /api/v1/finance/handoffs/{id}/review/

        Authenticated equivalent of the finance email review payload.
        """
        handoff = self.get_object()
        if not self._can_current_user_act_as_finance(handoff):
            return self._finance_permission_denied()

        subject_name = FinanceHandoffSerializer(handoff).data["subject_name"]
        base_data = {
            "action_type": "review",
            "is_expired": False,
            "is_used": not handoff.is_active(),
            "module": handoff.module,
            "subject_type": handoff.subject_type,
            "subject_name": subject_name,
            "handoff_status": handoff.status,
        }

        if handoff.module == "invoice":
            invoice_data = PublicFinanceTokenView()._build_invoice_payload(handoff, request)
            if invoice_data:
                base_data.update(invoice_data)
                return Response(InvoiceFinanceReviewSerializer(base_data).data)

        return Response(PublicFinanceTokenSerializer(base_data).data)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """POST /api/v1/finance/handoffs/{id}/approve/"""
        handoff = self.get_object()
        if not self._can_current_user_act_as_finance(handoff):
            return self._finance_permission_denied()

        serializer = FinanceApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            handoff, decision = approve_finance_handoff(
                handoff=handoff,
                reference_id=serializer.validated_data["reference_id"],
                note=serializer.validated_data.get("note", ""),
                actor=request.user,
            )
        except HandoffStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "handoff": FinanceHandoffSerializer(handoff).data,
            "decision": FinanceDecisionSerializer(decision).data,
        })

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """POST /api/v1/finance/handoffs/{id}/reject/"""
        handoff = self.get_object()
        if not self._can_current_user_act_as_finance(handoff):
            return self._finance_permission_denied()

        serializer = FinanceRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.validated_data.get("note", "")
        if handoff.module == "invoice" and not (note and note.strip()):
            return Response(
                {"detail": "Rejection reason is required for invoice finance review."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            handoff, decision = reject_finance_handoff(
                handoff=handoff,
                note=note,
                actor=request.user,
            )
        except HandoffStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "handoff": FinanceHandoffSerializer(handoff).data,
            "decision": FinanceDecisionSerializer(decision).data,
        })


# ---------------------------------------------------------------------------
# Public: token metadata
# ---------------------------------------------------------------------------

class PublicFinanceTokenView(APIView):
    """GET /api/v1/finance/public/{token}/"""
    permission_classes = [AllowAny]

    def get(self, request, token, *args, **kwargs):
        try:
            handoff = get_handoff_by_token(token)
        except TokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)

        try:
            action_token = FinanceActionToken.objects.select_related("handoff").get(token=token)
        except FinanceActionToken.DoesNotExist:
            return Response({"detail": "Token not found."}, status=status.HTTP_404_NOT_FOUND)

        base_data = {
            "action_type": action_token.action_type,
            "is_expired": action_token.is_expired(),
            "is_used": action_token.is_used(),
            "module": handoff.module,
            "subject_type": handoff.subject_type,
            "subject_name": self._get_subject_name(handoff),
            "handoff_status": handoff.status,
            "reject_token": self._get_paired_reject_token(action_token),
        }

        if handoff.module == "invoice":
            invoice_data = self._build_invoice_payload(handoff, request)
            if invoice_data:
                base_data.update(invoice_data)
                return Response(InvoiceFinanceReviewSerializer(base_data).data)

        return Response(PublicFinanceTokenSerializer(base_data).data)

    def _get_paired_reject_token(self, action_token):
        if action_token.action_type != "approve":
            return None
        return (
            action_token.handoff.action_tokens
            .filter(action_type="reject", used_at__isnull=True)
            .order_by("-created_at", "-id")
            .values_list("token", flat=True)
            .first()
        )

    def _get_subject_name(self, handoff):
        if handoff.module == "invoice":
            from apps.invoices.models import Invoice
            try:
                return Invoice.objects.get(pk=handoff.subject_id).title
            except Invoice.DoesNotExist:
                return f"Invoice {handoff.subject_id}"
        elif handoff.module == "campaign":
            from apps.campaigns.models import Campaign
            try:
                return Campaign.objects.get(pk=handoff.subject_id).name
            except Campaign.DoesNotExist:
                return f"Campaign {handoff.subject_id}"
        return f"{handoff.subject_type} {handoff.subject_id}"

    def _build_invoice_payload(self, handoff, request) -> dict | None:
        """Build rich invoice finance review payload for invoice handoffs."""
        from apps.invoices.models import Invoice
        from apps.invoices.services import can_user_record_invoice_payment

        try:
            invoice = Invoice.objects.select_related(
                "vendor",
                "vendor__onboarding_submission",
                "scope_node",
                "created_by",
            ).get(pk=handoff.subject_id)
        except Invoice.DoesNotExist:
            return None

        handoff_data = {
            "id": handoff.id,
            "status": handoff.status,
            "sent_at": handoff.sent_at,
            "created_at": handoff.created_at,
            "finance_reference_id": handoff.finance_reference_id,
            "recipient_count": 0,
            "recipient_emails": [],
        }
        try:
            from apps.finance.services import resolve_finance_recipients_for_handoff
            handoff_data["recipient_emails"] = resolve_finance_recipients_for_handoff(handoff)
            handoff_data["recipient_count"] = len(handoff_data["recipient_emails"])
        except Exception:
            pass

        invoice_data = {
            "id": invoice.id,
            "title": invoice.title,
            "amount": str(invoice.amount),
            "currency": invoice.currency,
            "status": invoice.status,
            "po_number": invoice.po_number,
            "vendor_invoice_number": invoice.vendor_invoice_number,
            "invoice_date": invoice.invoice_date,
            "due_date": invoice.due_date,
            "description": invoice.description,
            "scope_node_id": invoice.scope_node_id,
            "scope_node_name": invoice.scope_node.name if invoice.scope_node else None,
            "can_record_payment": can_user_record_invoice_payment(request.user, invoice),
            "created_at": invoice.created_at,
            "updated_at": invoice.updated_at,
        }

        def _first_value(*values):
            for value in values:
                if value not in (None, ""):
                    return value
            return None

        vendor_data = None
        vendor_submission = None
        if invoice.vendor:
            vendor = invoice.vendor
            vendor_submission = getattr(vendor, "onboarding_submission", None)

            def _vendor_value(field: str):
                return _first_value(
                    getattr(vendor, field, None),
                    getattr(vendor_submission, f"normalized_{field}", None) if vendor_submission else None,
                )

            vendor_data = {
                "id": vendor.id,
                "vendor_name": vendor.vendor_name,
                "email": _vendor_value("email"),
                "phone": _vendor_value("phone"),
                "gstin": _vendor_value("gstin"),
                "pan": _vendor_value("pan"),
                "sap_vendor_id": getattr(vendor, "sap_vendor_id", None),
                "preferred_payment_mode": _vendor_value("preferred_payment_mode"),
                "beneficiary_name": _vendor_value("beneficiary_name"),
                "beneficiary_account_number": _vendor_value("beneficiary_account_number"),
                "bank_name": _vendor_value("bank_name"),
                "bank_address": _vendor_value("bank_address"),
                "bank_email": _vendor_value("bank_email"),
                "account_number": _vendor_value("account_number"),
                "bank_account_number": _vendor_value("bank_account_number"),
                "bank_account_type": _vendor_value("bank_account_type"),
                "ifsc": _vendor_value("ifsc"),
                "micr_code": _vendor_value("micr_code"),
                "neft_code": _vendor_value("neft_code"),
                "bank_branch_address_line1": _vendor_value("bank_branch_address_line1"),
                "bank_branch_address_line2": _vendor_value("bank_branch_address_line2"),
                "bank_branch_city": _vendor_value("bank_branch_city"),
                "bank_branch_state": _vendor_value("bank_branch_state"),
                "bank_branch_country": _vendor_value("bank_branch_country"),
                "bank_branch_pincode": _vendor_value("bank_branch_pincode"),
                "bank_phone": _vendor_value("bank_phone"),
                "bank_fax": _vendor_value("bank_fax"),
            }

        documents_data = []
        try:
            from apps.invoices.models import InvoiceDocument, VendorInvoiceSubmission

            for sub in VendorInvoiceSubmission.objects.filter(final_invoice=invoice).order_by("-created_at")[:5]:
                file_url = request.build_absolute_uri(sub.source_file.url) if sub.source_file else None
                documents_data.append({
                    "id": -sub.id,
                    "file_name": sub.source_file_name or f"Submission-{sub.id}",
                    "document_type": "source_invoice",
                    "document_group": "invoice",
                    "uploaded_at": sub.created_at,
                    "url": file_url,
                })

            for doc in InvoiceDocument.objects.filter(invoice=invoice).order_by("-created_at"):
                file_url = request.build_absolute_uri(doc.file.url) if doc.file else None
                documents_data.append({
                    "id": doc.id,
                    "file_name": doc.file_name or f"Document-{doc.id}",
                    "document_type": doc.document_type,
                    "document_group": "invoice",
                    "uploaded_at": doc.created_at,
                    "url": file_url,
                })

            if vendor_submission:
                from apps.vendors.models import VENDOR_ATTACHMENT_DOCUMENT_TYPE_LABELS

                for attachment in vendor_submission.attachments.all().order_by("-created_at"):
                    file_url = request.build_absolute_uri(attachment.file.url) if attachment.file else (
                        attachment.file_url or None
                    )
                    label = VENDOR_ATTACHMENT_DOCUMENT_TYPE_LABELS.get(
                        attachment.document_type,
                        attachment.document_type or "Vendor Document",
                    )
                    documents_data.append({
                        "id": -1000000 - attachment.id,
                        "file_name": attachment.file_name or attachment.title or f"VendorDocument-{attachment.id}",
                        "document_type": label,
                        "document_group": "vendor",
                        "uploaded_at": attachment.created_at,
                        "url": file_url,
                    })
        except Exception:
            pass

        allocations_data = []
        try:
            from apps.invoices.models import InvoiceAllocation
            from apps.invoices.models import InvoiceAllocationStatus
            for alloc in InvoiceAllocation.objects.filter(
                invoice=invoice
            ).select_related("entity", "category", "subcategory", "campaign", "budget", "selected_approver"):
                allocations_data.append({
                    "id": alloc.id,
                    "entity_name": alloc.entity.name if alloc.entity else None,
                    "amount": str(alloc.amount),
                    "category_name": alloc.category.name if alloc.category else None,
                    "subcategory_name": alloc.subcategory.name if alloc.subcategory else None,
                    "campaign_name": alloc.campaign.name if alloc.campaign else None,
                    "budget_name": str(alloc.budget) if alloc.budget else None,
                    "selected_approver_email": (
                        alloc.selected_approver.email if alloc.selected_approver else None
                    ),
                    "status": alloc.status,
                    "note": alloc.note or None,
                })
        except Exception:
            pass

        workflow_data = None
        try:
            from apps.workflow.models import WorkflowInstance
            instance = WorkflowInstance.objects.filter(
                subject_type="invoice",
                subject_id=invoice.id,
            ).select_related("template_version__template").first()
            if instance:
                groups_data = []
                for ig in instance.instance_groups.select_related("step_group").prefetch_related(
                    "instance_steps__workflow_step",
                    "instance_steps__assigned_user",
                    "instance_steps__branches__assigned_user",
                ).order_by("display_order"):
                    steps_data = []
                    for step in ig.instance_steps.all():
                        steps_data.append({
                            "name": step.workflow_step.name if step.workflow_step_id else f"Step {step.id}",
                            "status": step.status,
                            "assigned_user_email": (
                                step.assigned_user.email if step.assigned_user else None
                            ),
                            "acted_at": step.acted_at,
                            "note": step.note or None,
                        })
                    branches_data = []
                    for branch in ig.instance_steps.all():
                        for br in branch.branches.all():
                            branches_data.append({
                                "entity_name": br.target_scope_node.name if br.target_scope_node else None,
                                "status": br.status,
                                "assigned_user_email": (
                                    br.assigned_user.email if br.assigned_user else None
                                ),
                                "acted_at": br.acted_at,
                                "note": br.note or None,
                            })
                    groups_data.append({
                        "name": ig.step_group.name,
                        "status": ig.status,
                        "display_order": ig.display_order,
                        "steps": steps_data,
                        "branches": branches_data,
                    })
                workflow_data = {
                    "instance_id": instance.id,
                    "status": instance.status,
                    "template_name": (
                        instance.template_version.template.name
                        if instance.template_version and instance.template_version.template
                        else None
                    ),
                    "version_number": (
                        instance.template_version.version_number
                        if instance.template_version else None
                    ),
                    "groups": groups_data,
                }
        except Exception:
            pass

        timeline_data = []
        try:
            from apps.workflow.models import WorkflowEvent
            for event in WorkflowEvent.objects.filter(
                instance__subject_type="invoice",
                instance__subject_id=invoice.id,
            ).select_related("actor_user").order_by("created_at")[:30]:
                timeline_data.append({
                    "event_type": event.event_type,
                    "actor_email": (
                        event.actor_user.email if event.actor_user else None
                    ),
                    "created_at": event.created_at,
                    "metadata": event.metadata or {},
                })
        except Exception:
            pass

        return {
            "handoff": handoff_data,
            "invoice": invoice_data,
            "vendor": vendor_data,
            "documents": documents_data,
            "allocations": allocations_data,
            "workflow": workflow_data,
            "timeline": timeline_data,
        }


# ---------------------------------------------------------------------------
# Public: approve via token
# ---------------------------------------------------------------------------

class PublicFinanceApproveView(APIView):
    """POST /api/v1/finance/public/{token}/approve/"""
    permission_classes = [AllowAny]

    def post(self, request, token, *args, **kwargs):
        serializer = FinanceApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            handoff, decision = finance_approve_handoff(
                token_str=token,
                reference_id=serializer.validated_data["reference_id"],
                note=serializer.validated_data.get("note", ""),
            )
        except TokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except HandoffStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "handoff": FinanceHandoffSerializer(handoff).data,
            "decision": FinanceDecisionSerializer(decision).data,
        })


# ---------------------------------------------------------------------------
# Public: reject via token
# ---------------------------------------------------------------------------

class PublicFinanceRejectView(APIView):
    """POST /api/v1/finance/public/{token}/reject/"""
    permission_classes = [AllowAny]

    def post(self, request, token, *args, **kwargs):
        serializer = FinanceRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.validated_data.get("note", "")

        # Invoice rejections must include a reason
        try:
            handoff = get_handoff_by_token(token)
        except TokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if handoff.module == "invoice" and not (note and note.strip()):
            return Response(
                {"detail": "Rejection reason is required for invoice finance review."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            handoff, decision = finance_reject_handoff(
                token_str=token,
                note=note,
            )
        except TokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except HandoffStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "handoff": FinanceHandoffSerializer(handoff).data,
            "decision": FinanceDecisionSerializer(decision).data,
        })
