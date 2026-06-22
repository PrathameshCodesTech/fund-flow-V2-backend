import mimetypes
import os

from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.vendors.models import (
    FinanceActionType,
    SubmissionStatus,
    Vendor,
    VendorAttachment,
    VendorFinanceActionToken,
    VendorInvitation,
    VendorOnboardingSubmission,
    VendorTrainingVideo,
)
from apps.vendors.services import (
    FinanceTokenError,
    InvitationExpiredError,
    InvitationNotFoundError,
    POMandate,
    SubmissionStateError,
    VendorStateError,
    approve_vendor_submission_finance,
    approve_vendor_marketing,
    attach_document,
    create_or_update_submission_from_excel,
    create_or_update_submission_from_manual,
    create_vendor_invitation,
    finance_approve_submission,
    finance_reject_submission,
    finalize_submission,
    get_invitation_by_token,
    reject_vendor_marketing,
    reject_vendor_submission_finance,
    remove_submission_attachment,
    reopen_submission,
    send_submission_to_finance,
)
from apps.vendors.route_services import (
    RouteAssigneeReplacementError,
    get_route_assignee_replacement_options,
    replace_route_assignee,
)
from apps.access.selectors import get_user_actionable_scope_ids, get_user_visible_scope_ids
from apps.access.services import user_can_act_on_scope_response
from apps.vendors.api.serializers import (
    FinanceApproveSerializer,
    FinanceRejectSerializer,
    FinalizeSerializer,
    ManualSubmissionSerializer,
    MarketingApproveSerializer,
    MarketingRejectSerializer,
    PublicFinanceTokenSerializer,
    PublicInvitationSerializer,
    ReopenSubmissionSerializer,
    RejectRevisionSerializer,
    SaveDraftRevisionSerializer,
    SendToFinanceSerializer,
    VendorAttachmentCreateSerializer,
    VendorAttachmentSerializer,
    VendorInvitationCreateSerializer,
    VendorInvitationSerializer,
    VendorProfileRevisionListSerializer,
    VendorProfileRevisionSerializer,
    VendorSerializer,
    VendorSubmissionSerializer,
    VendorUpdateSerializer,
    VendorSubmissionRouteSerializer,
    VendorSubmissionRouteCreateSerializer,
    VendorSubmissionRouteReplaceAssigneeSerializer,
    VendorSubmissionRouteUpdateSerializer,
    VendorSubmissionRouteVendorSerializer,
    VendorTrainingVideoSerializer,
)


# ---------------------------------------------------------------------------
# Internal authenticated endpoints
# ---------------------------------------------------------------------------

class VendorInvitationViewSet(viewsets.ModelViewSet):
    """
    Authenticated endpoint for managing vendor invitations.
    Filters: org, scope_node, status, vendor_email
    """
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = VendorInvitation.objects.select_related("org", "scope_node", "invited_by").filter(
            scope_node_id__in=visible_scope_ids
        )
        params = self.request.query_params
        if org_id := params.get("org"):
            qs = qs.filter(org_id=org_id)
        if scope_node_id := params.get("scope_node"):
            qs = qs.filter(scope_node_id=scope_node_id)
        if status_val := params.get("status"):
            qs = qs.filter(status=status_val)
        if email := params.get("vendor_email"):
            qs = qs.filter(vendor_email__icontains=email)
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return VendorInvitationCreateSerializer
        return VendorInvitationSerializer

    def create(self, request, *args, **kwargs):
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_response(request.user, scope_node_id, "create an invitation"):
                return err
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        invitation = create_vendor_invitation(
            org=d["org"],
            scope_node=d["scope_node"],
            vendor_email=d["vendor_email"],
            invited_by=request.user,
            vendor_name_hint=d.get("vendor_name_hint", ""),
            expires_at=d.get("expires_at"),
        )
        return Response(VendorInvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        invitation = self.get_object()
        # Actionable scope check on invitation's scope_node
        if err := user_can_act_on_scope_response(request.user, invitation.scope_node_id, "cancel this invitation"):
            return err
        from apps.vendors.models import InvitationStatus
        if invitation.status in (InvitationStatus.CANCELLED, InvitationStatus.EXPIRED):
            return Response(
                {"detail": f"Invitation is already {invitation.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        invitation.status = InvitationStatus.CANCELLED
        invitation.save(update_fields=["status", "updated_at"])
        return Response(VendorInvitationSerializer(invitation).data)


class VendorSubmissionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Internal read + action endpoints for vendor onboarding submissions.
    Filters: org, scope_node, status, invitation, normalized_vendor_name, normalized_email
    """
    permission_classes = [IsAuthenticated]
    serializer_class = VendorSubmissionSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = VendorOnboardingSubmission.objects.select_related(
            "invitation__org", "invitation__scope_node"
        ).filter(invitation__scope_node_id__in=visible_scope_ids)
        params = self.request.query_params
        if org_id := params.get("org"):
            qs = qs.filter(invitation__org_id=org_id)
        if scope_node_id := params.get("scope_node"):
            qs = qs.filter(invitation__scope_node_id=scope_node_id)
        if status_val := params.get("status"):
            qs = qs.filter(status=status_val)
        if (params.get("finance_reviewed") or "").strip().lower() in {"1", "true", "yes"}:
            qs = qs.filter(finance_decisions__isnull=False).distinct()
        if inv_id := params.get("invitation"):
            qs = qs.filter(invitation_id=inv_id)
        if name := params.get("normalized_vendor_name"):
            qs = qs.filter(normalized_vendor_name__icontains=name)
        if email := params.get("normalized_email"):
            qs = qs.filter(normalized_email__icontains=email)
        return qs

    def _static_finance_recipient_emails(self) -> set[str]:
        from django.conf import settings

        recipients = getattr(
            settings,
            "VENDOR_FINANCE_RECIPIENTS",
            getattr(settings, "VENDOR_FINANCE_EMAIL_RECIPIENTS", []),
        )
        if isinstance(recipients, str):
            recipients = [recipients]
        return {r.strip().lower() for r in recipients if r}

    def _user_has_finance_role_for_submission(self, request, submission) -> bool:
        from apps.access.models import UserRoleAssignment
        from apps.core.services import get_ancestors
        from django.conf import settings

        scope_node = submission.invitation.scope_node
        if not scope_node:
            return False

        scope_ids = [scope_node.id]
        for ancestor in get_ancestors(scope_node):
            if ancestor.id not in scope_ids:
                scope_ids.append(ancestor.id)

        role_codes = set(getattr(settings, "FINANCE_ROLE_CODES", {"finance_team"}))
        return UserRoleAssignment.objects.filter(
            user=request.user,
            role__code__in=role_codes,
            role__is_active=True,
            scope_node_id__in=scope_ids,
        ).exists()

    def _can_current_user_act_as_finance(self, request, submission) -> bool:
        if request.user.is_superuser:
            return True
        email = (getattr(request.user, "email", "") or "").strip().lower()
        if email and email in self._static_finance_recipient_emails():
            return True
        return self._user_has_finance_role_for_submission(request, submission)

    def _finance_permission_denied(self):
        return Response(
            {"detail": "You are not a finance recipient for this vendor submission."},
            status=status.HTTP_403_FORBIDDEN,
        )

    def _get_review_token_for_submission(self, submission):
        return (
            submission.finance_tokens
            .filter(action_type=FinanceActionType.APPROVE, used_at__isnull=True)
            .order_by("-created_at", "-id")
            .first()
            or submission.finance_tokens
            .filter(action_type=FinanceActionType.APPROVE)
            .order_by("-created_at", "-id")
            .first()
        )

    @action(detail=True, methods=["post"], url_path="send-to-finance")
    def send_to_finance(self, request, pk=None):
        submission = self.get_object()
        # Actionable scope check via invitation's scope_node
        if err := user_can_act_on_scope_response(request.user, submission.invitation.scope_node_id, "send submission to finance"):
            return err
        try:
            updated = send_submission_to_finance(submission, triggered_by=request.user)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorSubmissionSerializer(updated).data)

    @action(detail=True, methods=["get"], url_path="finance-review")
    def finance_review(self, request, pk=None):
        """GET /api/v1/vendors/submissions/{id}/finance-review/"""
        submission = self.get_object()
        if not self._can_current_user_act_as_finance(request, submission):
            return self._finance_permission_denied()

        token = self._get_review_token_for_submission(submission)
        if not token:
            return Response(
                {"detail": "No finance review token exists for this submission."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(PublicFinanceTokenSerializer(token, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="finance-approve")
    def finance_approve(self, request, pk=None):
        """POST /api/v1/vendors/submissions/{id}/finance-approve/"""
        submission = self.get_object()
        if not self._can_current_user_act_as_finance(request, submission):
            return self._finance_permission_denied()

        serializer = FinanceApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            submission, vendor = approve_vendor_submission_finance(
                submission=submission,
                sap_vendor_id=serializer.validated_data["sap_vendor_id"],
                note=serializer.validated_data.get("note", ""),
                actor=request.user,
            )
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "submission": VendorSubmissionSerializer(submission).data,
            "vendor": VendorSerializer(vendor).data,
        })

    @action(detail=True, methods=["post"], url_path="finance-reject")
    def finance_reject(self, request, pk=None):
        """POST /api/v1/vendors/submissions/{id}/finance-reject/"""
        submission = self.get_object()
        if not self._can_current_user_act_as_finance(request, submission):
            return self._finance_permission_denied()

        serializer = FinanceRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            submission = reject_vendor_submission_finance(
                submission=submission,
                note=serializer.validated_data.get("note", ""),
                actor=request.user,
            )
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(VendorSubmissionSerializer(submission).data)

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None):
        submission = self.get_object()
        # Actionable scope check via invitation's scope_node
        if err := user_can_act_on_scope_response(request.user, submission.invitation.scope_node_id, "reopen this submission"):
            return err
        serializer = ReopenSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated = reopen_submission(
                submission,
                reopened_by=request.user,
                note=serializer.validated_data.get("note", ""),
            )
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorSubmissionSerializer(updated).data)


class VendorAttachmentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Internal read-only attachment listing.
    Filters: submission
    """
    permission_classes = [IsAuthenticated]
    serializer_class = VendorAttachmentSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = VendorAttachment.objects.select_related("submission", "uploaded_by").filter(
            submission__invitation__scope_node_id__in=visible_scope_ids
        )
        if sub_id := self.request.query_params.get("submission"):
            qs = qs.filter(submission_id=sub_id)
        return qs


class VendorViewSet(viewsets.ModelViewSet):
    """
    Vendor master CRUD + marketing approval actions.
    Filters: org, scope_node, operational_status, marketing_status, po_mandate_enabled
    """
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = Vendor.objects.select_related(
            "org", "scope_node", "onboarding_submission", "approved_by_marketing"
        ).filter(scope_node_id__in=visible_scope_ids)
        params = self.request.query_params
        if org_id := params.get("org"):
            qs = qs.filter(org_id=org_id)
        if scope_node_id := params.get("scope_node"):
            qs = qs.filter(scope_node_id=scope_node_id)
        if op_status := params.get("operational_status"):
            qs = qs.filter(operational_status=op_status)
        if mkt_status := params.get("marketing_status"):
            qs = qs.filter(marketing_status=mkt_status)
        if po_mandate := params.get("po_mandate_enabled"):
            qs = qs.filter(po_mandate_enabled=(po_mandate.lower() in ("true", "1", "yes")))
        return qs

    def get_serializer_class(self):
        if self.action == "partial_update":
            return VendorUpdateSerializer
        return VendorSerializer

    def partial_update(self, request, *args, **kwargs):
        vendor = self.get_object()
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "update this vendor"):
            return err
        return super().partial_update(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="marketing-approve")
    def marketing_approve(self, request, pk=None):
        vendor = self.get_object()
        # Actionable scope check on vendor's scope_node
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "approve vendor marketing"):
            return err
        serializer = MarketingApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated = approve_vendor_marketing(
                vendor,
                approved_by=request.user,
            )
        except VendorStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorSerializer(updated).data)

    @action(detail=True, methods=["post"], url_path="resend-activation")
    def resend_activation(self, request, pk=None):
        """
        POST /api/v1/vendors/{id}/resend-activation/

        Resend the vendor portal activation email.
        - Vendor must be active.
        - Vendor must have an email.
        - Creates/reuses portal user and UserVendorAssignment.
        - Invalidates old unused tokens, creates fresh token.
        - Sends activation email (mandatory — returns 500 on failure).
        - Returns structured result.
        """
        vendor = self.get_object()
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "resend vendor activation"):
            return err

        from apps.vendors.services import OperationalStatus, VendorStateError, send_vendor_activation_for_vendor

        if vendor.operational_status != OperationalStatus.ACTIVE:
            return Response(
                {"detail": f"Vendor is in status '{vendor.operational_status}' — must be 'active' to resend activation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not vendor.email and not (vendor.onboarding_submission and vendor.onboarding_submission.normalized_email):
            return Response(
                {"detail": "Vendor has no email address. Cannot send activation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = send_vendor_activation_for_vendor(vendor, actor=request.user)
        except VendorStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response(
                {"detail": f"Failed to send activation email: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "detail": "Activation email sent.",
            "vendor_id": str(vendor.pk),
            "email": result["email"],
            "user_created": result["user_created"],
            "assignment_created": result["assignment_created"],
            "token_created": result["token_created"],
        })

    def partial_update(self, request, *args, **kwargs):
        vendor = self.get_object()
        # Actionable scope check on vendor's scope_node
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "reject vendor marketing"):
            return err
        serializer = MarketingRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated = reject_vendor_marketing(
                vendor,
                rejected_by=request.user,
                note=serializer.validated_data.get("note", ""),
            )
        except VendorStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorSerializer(updated).data)


# ---------------------------------------------------------------------------
# Public token-based endpoints
# ---------------------------------------------------------------------------

class PublicInvitationView(APIView):
    """GET /api/v1/vendors/public/invitations/{token}/"""
    permission_classes = [AllowAny]

    def get(self, request, token, *args, **kwargs):
        try:
            invitation = get_invitation_by_token(token)
        except InvitationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except InvitationExpiredError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_410_GONE)
        return Response(PublicInvitationSerializer(invitation).data)


class PublicInvitationSubmissionView(APIView):
    """GET /api/v1/vendors/public/invitations/{token}/submission/"""

    permission_classes = [AllowAny]

    def get(self, request, token, *args, **kwargs):
        try:
            invitation = get_invitation_by_token(token)
        except InvitationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except InvitationExpiredError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_410_GONE)

        submission = invitation.submissions.order_by("-created_at").first()
        if not submission:
            return Response({"detail": "No submission found for this invitation."}, status=status.HTTP_404_NOT_FOUND)

        return Response(VendorSubmissionSerializer(submission).data)


class PublicInvitationSubmitManualView(APIView):
    """POST /api/v1/vendors/public/invitations/{token}/submit-manual/"""
    permission_classes = [AllowAny]

    def post(self, request, token, *args, **kwargs):
        try:
            invitation = get_invitation_by_token(token)
        except InvitationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except InvitationExpiredError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_410_GONE)

        serializer = ManualSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data.get("data", {})
        finalize = serializer.validated_data.get("finalize", False)

        try:
            submission = create_or_update_submission_from_manual(
                invitation, payload, finalize=finalize
            )
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        http_status_code = status.HTTP_201_CREATED if not submission.pk else status.HTTP_200_OK
        return Response(VendorSubmissionSerializer(submission).data, status=http_status_code)


class PublicInvitationSubmitExcelView(APIView):
    """POST /api/v1/vendors/public/invitations/{token}/submit-excel/"""
    permission_classes = [AllowAny]
    parser_classes_override = None  # Accept multipart via DEFAULT_PARSER_CLASSES

    def post(self, request, token, *args, **kwargs):
        try:
            invitation = get_invitation_by_token(token)
        except InvitationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except InvitationExpiredError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_410_GONE)

        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response({"detail": "file is required."}, status=status.HTTP_400_BAD_REQUEST)

        finalize = request.data.get("finalize", "false").lower() in ("true", "1", "yes")

        try:
            submission = create_or_update_submission_from_excel(
                invitation, file_obj, finalize=finalize
            )
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"detail": f"Excel parse error: {exc}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(VendorSubmissionSerializer(submission).data, status=status.HTTP_200_OK)


class PublicInvitationAttachView(APIView):
    """POST /api/v1/vendors/public/invitations/{token}/attachments/ (multipart)"""
    permission_classes = [AllowAny]

    def post(self, request, token, *args, **kwargs):
        try:
            invitation = get_invitation_by_token(token)
        except InvitationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except InvitationExpiredError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_410_GONE)

        submission = invitation.submissions.filter(
            status__in=["draft", "reopened"]
        ).first()
        if not submission:
            return Response(
                {"detail": "No editable submission found for this invitation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        file_obj = request.FILES.get("file")
        title = request.data.get("title", "").strip()
        document_type = request.data.get("document_type", "").strip()

        if not title:
            return Response({"detail": "title is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not file_obj:
            return Response({"detail": "file is required."}, status=status.HTTP_400_BAD_REQUEST)

        # 10 MB limit
        max_bytes = 10 * 1024 * 1024
        if file_obj.size > max_bytes:
            return Response(
                {"detail": "File size exceeds 10 MB limit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        allowed_extensions = {
            ".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls",
            ".doc", ".docx", ".txt", ".csv",
        }
        if document_type == "msme_declaration_form":
            allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".docx"}
        from pathlib import Path
        ext = Path(file_obj.name).suffix.lower()
        if ext not in allowed_extensions:
            return Response(
                {"detail": f"File type '{ext}' is not allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            attachment = attach_document(
                submission=submission,
                title=title,
                file_obj=file_obj,
                document_type=document_type,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(VendorAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)


class PublicInvitationAttachmentDetailView(APIView):
    """DELETE /api/v1/vendors/public/invitations/{token}/attachments/{attachment_id}/"""
    permission_classes = [AllowAny]

    def delete(self, request, token, attachment_id, *args, **kwargs):
        try:
            invitation = get_invitation_by_token(token)
        except InvitationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except InvitationExpiredError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_410_GONE)

        submission = invitation.submissions.filter(
            status__in=["draft", "reopened"]
        ).first()
        if not submission:
            return Response(
                {"detail": "No editable submission found for this invitation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            remove_submission_attachment(submission, attachment_id)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)

        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicInvitationFinalizeView(APIView):
    """POST /api/v1/vendors/public/invitations/{token}/finalize/"""
    permission_classes = [AllowAny]

    def post(self, request, token, *args, **kwargs):
        try:
            invitation = get_invitation_by_token(token)
        except InvitationNotFoundError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except InvitationExpiredError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_410_GONE)

        submission = invitation.submissions.filter(
            status__in=["draft", "reopened", "finance_rejected"]
        ).first()
        if not submission:
            return Response(
                {"detail": "No editable submission to finalize."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            updated = finalize_submission(submission)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(VendorSubmissionSerializer(updated).data)


# ---------------------------------------------------------------------------
# Public finance action endpoints
# ---------------------------------------------------------------------------

class PublicFinanceActionView(APIView):
    """GET /api/v1/vendors/public/finance/{token}/"""
    permission_classes = [AllowAny]

    def get(self, request, token, *args, **kwargs):
        try:
            action_token = VendorFinanceActionToken.objects.select_related(
                "submission"
            ).get(token=token)
        except VendorFinanceActionToken.DoesNotExist:
            return Response({"detail": "Token not found."}, status=status.HTTP_404_NOT_FOUND)
        if action_token.is_used():
            return Response(
                {"detail": "This finance review has already been completed."},
                status=status.HTTP_410_GONE,
            )
        if action_token.is_expired():
            return Response(
                {"detail": "This finance review link has expired."},
                status=status.HTTP_410_GONE,
            )
        if action_token.submission.status not in (
            SubmissionStatus.SENT_TO_FINANCE,
            SubmissionStatus.REOPENED,
        ):
            return Response(
                {"detail": "This finance review has already been completed."},
                status=status.HTTP_410_GONE,
            )
        return Response(PublicFinanceTokenSerializer(action_token).data)


class PublicFinanceApproveView(APIView):
    """POST /api/v1/vendors/public/finance/{token}/approve/"""
    permission_classes = [AllowAny]

    def post(self, request, token, *args, **kwargs):
        serializer = FinanceApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            submission, vendor = finance_approve_submission(
                token_str=token,
                sap_vendor_id=serializer.validated_data["sap_vendor_id"],
                note=serializer.validated_data.get("note", ""),
            )
        except FinanceTokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "submission": VendorSubmissionSerializer(submission).data,
            "vendor": VendorSerializer(vendor).data,
        })


class PublicFinanceRejectView(APIView):
    """POST /api/v1/vendors/public/finance/{token}/reject/"""
    permission_classes = [AllowAny]

    def post(self, request, token, *args, **kwargs):
        serializer = FinanceRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            submission = finance_reject_submission(
                token_str=token,
                note=serializer.validated_data.get("note", ""),
            )
        except FinanceTokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(VendorSubmissionSerializer(submission).data)


# ---------------------------------------------------------------------------
# Finance download helpers (token-gated, no raw paths exposed)
# ---------------------------------------------------------------------------

def _resolve_finance_token_for_download(token_str: str):
    """Return VendorFinanceActionToken or raise Http404."""
    try:
        token = VendorFinanceActionToken.objects.select_related("submission").get(
            token=token_str
        )
    except VendorFinanceActionToken.DoesNotExist:
        raise Http404("Finance token not found.")
    if token.is_expired():
        raise Http404("Finance token has expired.")
    if token.is_used():
        raise Http404("Finance token has already been used.")
    return token


def _file_response(file_path: str, download_name: str) -> FileResponse:
    if not file_path or not os.path.isfile(file_path):
        raise Http404("File not available.")
    content_type, _ = mimetypes.guess_type(file_path)
    content_type = content_type or "application/octet-stream"
    fh = open(file_path, "rb")
    response = FileResponse(fh, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return response


class PublicFinanceDownloadExportView(APIView):
    """GET /api/v1/vendors/public/finance/{token}/download/export-excel/"""
    permission_classes = [AllowAny]

    def get(self, request, token, *args, **kwargs):
        tok = _resolve_finance_token_for_download(token)
        path = tok.submission.exported_excel_file
        vendor = (tok.submission.normalized_vendor_name or "vendor").replace(" ", "_")
        return _file_response(path, f"VRF_{vendor}_#{tok.submission_id}.xlsx")


class PublicFinanceDownloadSourceView(APIView):
    """GET /api/v1/vendors/public/finance/{token}/download/source-excel/"""
    permission_classes = [AllowAny]

    def get(self, request, token, *args, **kwargs):
        tok = _resolve_finance_token_for_download(token)
        path = tok.submission.source_excel_file
        return _file_response(path, f"source_upload_#{tok.submission_id}.xlsx")


class PublicFinanceDownloadAttachmentView(APIView):
    """GET /api/v1/vendors/public/finance/{token}/download/attachment/{attachment_id}/"""
    permission_classes = [AllowAny]

    def get(self, request, token, attachment_id, *args, **kwargs):
        tok = _resolve_finance_token_for_download(token)
        try:
            att = tok.submission.attachments.get(pk=attachment_id)
        except VendorAttachment.DoesNotExist:
            raise Http404("Attachment not found.")
        if att.file:
            response = FileResponse(
                att.file.open("rb"),
                content_type=mimetypes.guess_type(att.file.name)[0] or "application/octet-stream",
            )
            response["Content-Disposition"] = f'attachment; filename="{att.file_name or att.file.name}"'
            return response
        raise Http404("No file stored for this attachment.")


# ---------------------------------------------------------------------------
# Vendor portal activation endpoints (public, token-gated)
# ---------------------------------------------------------------------------

class PublicVendorActivateValidateView(APIView):
    """GET /api/v1/vendors/public/activate/{uid}/{token}/"""
    permission_classes = [AllowAny]

    def get(self, request, uid, token):
        from apps.vendors.models import VendorActivationToken
        try:
            act = VendorActivationToken.objects.get(uid=uid, token=token)
        except VendorActivationToken.DoesNotExist:
            return Response({"detail": "Invalid activation token."}, status=status.HTTP_404_NOT_FOUND)
        if act.is_expired():
            return Response({"detail": "This activation link has expired."}, status=status.HTTP_410_GONE)
        if act.is_used():
            return Response({"detail": "This activation link has already been used."}, status=status.HTTP_410_GONE)

        # Fetch user and vendor name
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            user = User.objects.get(pk=int(uid))
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        vendor_name = None
        assignment = user.vendor_assignments.filter(is_active=True).first()
        if assignment:
            vendor_name = assignment.vendor.vendor_name

        return Response({
            "vendor_name": vendor_name or user.get_full_name() or user.email,
            "email": user.email,
        })


class PublicVendorActivateSetPasswordView(APIView):
    """POST /api/v1/vendors/public/activate/{uid}/{token}/set-password/"""
    permission_classes = [AllowAny]

    def post(self, request, uid, token):
        from apps.vendors.models import VendorActivationToken
        try:
            act = VendorActivationToken.objects.get(uid=uid, token=token)
        except VendorActivationToken.DoesNotExist:
            return Response({"detail": "Invalid activation token."}, status=status.HTTP_404_NOT_FOUND)
        if act.is_expired():
            return Response({"detail": "This activation link has expired."}, status=status.HTTP_410_GONE)
        if act.is_used():
            return Response({"detail": "This activation link has already been used."}, status=status.HTTP_410_GONE)

        password = request.data.get("password", "").strip()
        if not password or len(password) < 8:
            return Response(
                {"detail": "Password must be at least 8 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from django.contrib.auth import get_user_model
        from django.utils import timezone
        User = get_user_model()
        try:
            user = User.objects.get(pk=int(uid))
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        user.set_password(password)
        user.is_active = True
        user.save(update_fields=["password", "is_active"])

        # Mark token used
        act.used_at = timezone.now()
        act.save(update_fields=["used_at"])

        return Response({"detail": "Password set successfully. You can now log in."})


# ---------------------------------------------------------------------------
# Authenticated vendor portal endpoint
# ---------------------------------------------------------------------------

class MyVendorView(APIView):
    """
    GET /api/v1/vendors/my-vendor/

    Returns the Vendor bound to the authenticated user via UserVendorAssignment.
    Only accessible to users with an active assignment.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.vendors.models import UserVendorAssignment, Vendor
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .select_related("vendor", "vendor__org", "vendor__scope_node")
            .first()
        )
        if not assignment:
            return Response(
                {"detail": "No vendor account is linked to your profile."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from apps.vendors.api.serializers import VendorSerializer
        return Response(VendorSerializer(assignment.vendor).data)


# ---------------------------------------------------------------------------
# VendorSubmissionRoute — internal CRUD
# ---------------------------------------------------------------------------

class VendorSubmissionRouteViewSet(viewsets.ModelViewSet):
    """
    Internal admin/config CRUD for VendorSubmissionRoute.

    Access gate: user must have a visible-scope assignment that covers the
    workflow_template's scope_node (list/retrieve), and an actionable-scope
    assignment for write operations (create/update).  Vendor portal users have
    no internal scope assignments, so they are implicitly excluded.

    GET    /api/v1/vendors/send-to-options/       — list (filter ?org=, ?is_active=)
    POST   /api/v1/vendors/send-to-options/       — create
    GET    /api/v1/vendors/send-to-options/{id}/  — retrieve
    PATCH  /api/v1/vendors/send-to-options/{id}/  — update / deactivate
    """
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        from apps.vendors.models import VendorSubmissionRoute
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = VendorSubmissionRoute.objects.select_related(
            "org", "workflow_template", "workflow_template__scope_node"
        ).filter(
            workflow_template__scope_node_id__in=visible_scope_ids
        ).order_by("display_order", "label")

        params = self.request.query_params
        if org_id := params.get("org"):
            qs = qs.filter(org_id=org_id)
        if is_active := params.get("is_active"):
            qs = qs.filter(is_active=(is_active.lower() in ("true", "1", "yes")))
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return VendorSubmissionRouteCreateSerializer
        if self.action == "partial_update":
            return VendorSubmissionRouteUpdateSerializer
        return VendorSubmissionRouteSerializer

    def create(self, request, *args, **kwargs):
        serializer = VendorSubmissionRouteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        template = serializer.validated_data["workflow_template"]
        if err := user_can_act_on_scope_response(
            request.user, template.scope_node_id,
            "create send-to route config",
        ):
            return err
        route = serializer.save()
        return Response(
            VendorSubmissionRouteSerializer(route).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        route = self.get_object()
        # Gate on the existing template's scope node before accepting any payload
        if err := user_can_act_on_scope_response(
            request.user, route.workflow_template.scope_node_id,
            "update send-to route config",
        ):
            return err
        serializer = VendorSubmissionRouteUpdateSerializer(
            route, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        route = serializer.save()
        return Response(VendorSubmissionRouteSerializer(route).data)

    @action(detail=True, methods=["get"], url_path="replacement-options")
    def replacement_options(self, request, pk=None):
        route = self.get_object()
        try:
            payload = get_route_assignee_replacement_options(route)
        except RouteAssigneeReplacementError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)

    @action(detail=True, methods=["post"], url_path="replace-assignee")
    def replace_assignee(self, request, pk=None):
        route = self.get_object()
        if err := user_can_act_on_scope_response(
            request.user,
            route.workflow_template.scope_node_id,
            "replace a send-to route assignee",
        ):
            return err

        serializer = VendorSubmissionRouteReplaceAssigneeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated_route, new_version, affected_count = replace_route_assignee(
                route=route,
                old_user=serializer.validated_data["old_user"],
                new_user=serializer.validated_data["new_user"],
                new_label=serializer.validated_data["label"],
                actor=request.user,
            )
        except RouteAssigneeReplacementError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "route": VendorSubmissionRouteSerializer(updated_route).data,
            "published_version_id": new_version.pk,
            "published_version_number": new_version.version_number,
            "affected_step_count": affected_count,
        })


# ---------------------------------------------------------------------------
# VendorSendToOptionsView — vendor-facing read-only list
# ---------------------------------------------------------------------------

class VendorSendToOptionsView(APIView):
    """
    GET /api/v1/vendors/vendor-send-to-options/

    Returns active VendorSubmissionRoute options visible to the authenticated
    vendor user (filtered to their org).  Only id, code, label, display_order
    are returned — no template internals are exposed.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.vendors.models import UserVendorAssignment, VendorSubmissionRoute
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .select_related("vendor__org")
            .first()
        )
        if not assignment:
            return Response(
                {"detail": "No active vendor account found."},
                status=status.HTTP_403_FORBIDDEN,
            )
        routes = VendorSubmissionRoute.objects.filter(
            org=assignment.vendor.org,
            is_active=True,
        ).order_by("display_order", "label")
        return Response(VendorSubmissionRouteVendorSerializer(routes, many=True).data)


# ---------------------------------------------------------------------------
# Vendor Portal — profile revision endpoints (vendor user)
# ---------------------------------------------------------------------------

class VendorPortalProfileView(APIView):
    """
    GET /api/v1/vendors/portal/profile/
    Returns the live profile snapshot for the authenticated vendor.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.vendors.models import UserVendorAssignment
        from apps.vendors.services import build_vendor_live_snapshot
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .select_related("vendor__onboarding_submission")
            .first()
        )
        if not assignment:
            return Response({"detail": "No vendor account linked."}, status=status.HTTP_404_NOT_FOUND)
        vendor = assignment.vendor
        snapshot = build_vendor_live_snapshot(vendor)
        documents = []
        if vendor.onboarding_submission_id:
            documents = VendorAttachmentSerializer(
                vendor.onboarding_submission.attachments.all(),
                many=True,
                context={"request": request},
            ).data
        return Response({
            "vendor_id": vendor.pk,
            "vendor_name": vendor.vendor_name,
            "profile_change_pending": vendor.profile_change_pending,
            "profile_hold_reason": vendor.profile_hold_reason,
            "snapshot": snapshot,
            "documents": documents,
        })


class VendorPortalTrainingVideoView(APIView):
    """
    GET /api/v1/vendors/portal/training-video/
    Returns the latest active training video for authenticated vendor users.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.vendors.models import UserVendorAssignment

        has_vendor_account = UserVendorAssignment.objects.filter(
            user=request.user,
            is_active=True,
        ).exists()
        if not has_vendor_account:
            return Response({"detail": "No vendor account linked."}, status=status.HTTP_404_NOT_FOUND)

        video = VendorTrainingVideo.objects.filter(is_active=True).first()
        return Response({
            "video": (
                VendorTrainingVideoSerializer(video, context={"request": request}).data
                if video
                else None
            )
        })


class VendorPortalProfileRevisionView(APIView):
    """
    GET  /api/v1/vendors/portal/profile/revision/   — get or create editable revision
    POST /api/v1/vendors/portal/profile/revision/save-draft/   — save draft
    POST /api/v1/vendors/portal/profile/revision/submit/       — submit
    """
    permission_classes = [IsAuthenticated]

    def _get_vendor(self, request):
        from apps.vendors.models import UserVendorAssignment
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .select_related("vendor__onboarding_submission")
            .first()
        )
        if not assignment:
            return None
        return assignment.vendor

    def get(self, request):
        from apps.vendors.services import get_or_create_editable_profile_revision
        vendor = self._get_vendor(request)
        if not vendor:
            return Response({"detail": "No vendor account linked."}, status=status.HTTP_404_NOT_FOUND)
        revision = get_or_create_editable_profile_revision(vendor, actor=request.user)
        return Response(VendorProfileRevisionSerializer(revision).data)


class VendorPortalSaveDraftRevisionView(APIView):
    """POST /api/v1/vendors/portal/profile/revision/save-draft/"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.vendors.models import UserVendorAssignment, VendorProfileRevisionStatus
        from apps.vendors.services import get_or_create_editable_profile_revision, save_draft_profile_revision
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .select_related("vendor__onboarding_submission")
            .first()
        )
        if not assignment:
            return Response({"detail": "No vendor account linked."}, status=status.HTTP_404_NOT_FOUND)
        vendor = assignment.vendor
        serializer = SaveDraftRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        revision = get_or_create_editable_profile_revision(vendor, actor=request.user)
        try:
            updated = save_draft_profile_revision(
                revision,
                proposed_snapshot=serializer.validated_data["proposed_snapshot"],
                actor=request.user,
            )
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorProfileRevisionSerializer(updated).data)


class VendorPortalSubmitRevisionView(APIView):
    """POST /api/v1/vendors/portal/profile/revision/submit/"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.vendors.models import UserVendorAssignment, VendorProfileRevisionStatus
        from apps.vendors.services import (
            get_or_create_editable_profile_revision, submit_profile_revision, SubmissionStateError
        )
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .select_related("vendor__onboarding_submission")
            .first()
        )
        if not assignment:
            return Response({"detail": "No vendor account linked."}, status=status.HTTP_404_NOT_FOUND)
        vendor = assignment.vendor
        from apps.vendors.models import VendorProfileRevision
        revision = vendor.profile_revisions.filter(
            status__in=[VendorProfileRevisionStatus.DRAFT, VendorProfileRevisionStatus.REOPENED]
        ).order_by("-created_at").first()
        if not revision:
            return Response({"detail": "No editable revision found. Save a draft first."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            updated = submit_profile_revision(revision, actor=request.user)
        except (SubmissionStateError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorProfileRevisionSerializer(updated).data)


class VendorPortalRevisionHistoryView(APIView):
    """GET /api/v1/vendors/portal/profile/revisions/"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.vendors.models import UserVendorAssignment
        assignment = (
            UserVendorAssignment.objects
            .filter(user=request.user, is_active=True)
            .select_related("vendor")
            .first()
        )
        if not assignment:
            return Response({"detail": "No vendor account linked."}, status=status.HTTP_404_NOT_FOUND)
        revisions = assignment.vendor.profile_revisions.order_by("-created_at")
        return Response(VendorProfileRevisionListSerializer(revisions, many=True).data)


# ---------------------------------------------------------------------------
# Internal — profile revision review endpoints
# ---------------------------------------------------------------------------

class VendorProfileRevisionViewSet(viewsets.ViewSet):
    """
    Internal endpoints for reviewing vendor profile revisions.

    GET    /api/v1/vendors/{vendor_pk}/profile-revisions/         — list
    GET    /api/v1/vendors/{vendor_pk}/profile-revisions/{pk}/    — detail
    POST   .../finance-approve/                                   — finance approve
    POST   .../finance-reject/                                    — finance reject
    POST   .../reopen/                                            — reopen (back to vendor)
    POST   .../apply/                                             — apply approved revision
    POST   .../apply/                                             — apply directly (skip marketing)
    POST   .../cancel/                                            — cancel
    """
    permission_classes = [IsAuthenticated]

    def _get_vendor(self, request, vendor_pk):
        visible_scope_ids = get_user_visible_scope_ids(request.user)
        try:
            return Vendor.objects.get(pk=vendor_pk, scope_node_id__in=visible_scope_ids)
        except Vendor.DoesNotExist:
            return None

    def _get_revision(self, vendor, pk):
        from apps.vendors.models import VendorProfileRevision
        try:
            return VendorProfileRevision.objects.select_related("vendor").get(pk=pk, vendor=vendor)
        except VendorProfileRevision.DoesNotExist:
            return None

    def list(self, request, vendor_pk=None):
        vendor = self._get_vendor(request, vendor_pk)
        if not vendor:
            return Response({"detail": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)
        revisions = vendor.profile_revisions.order_by("-created_at")
        return Response(VendorProfileRevisionListSerializer(revisions, many=True).data)

    def retrieve(self, request, vendor_pk=None, pk=None):
        vendor = self._get_vendor(request, vendor_pk)
        if not vendor:
            return Response({"detail": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)
        revision = self._get_revision(vendor, pk)
        if not revision:
            return Response({"detail": "Revision not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(VendorProfileRevisionSerializer(revision).data)

    @action(detail=True, methods=["post"], url_path="finance-approve")
    def finance_approve(self, request, vendor_pk=None, pk=None):
        from apps.vendors.services import finance_approve_profile_revision, SubmissionStateError
        vendor = self._get_vendor(request, vendor_pk)
        if not vendor:
            return Response({"detail": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "finance approve profile revision"):
            return err
        revision = self._get_revision(vendor, pk)
        if not revision:
            return Response({"detail": "Revision not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            updated = finance_approve_profile_revision(revision, actor=request.user)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorProfileRevisionSerializer(updated).data)

    @action(detail=True, methods=["post"], url_path="finance-reject")
    def finance_reject(self, request, vendor_pk=None, pk=None):
        from apps.vendors.services import finance_reject_profile_revision, SubmissionStateError
        vendor = self._get_vendor(request, vendor_pk)
        if not vendor:
            return Response({"detail": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "finance reject profile revision"):
            return err
        revision = self._get_revision(vendor, pk)
        if not revision:
            return Response({"detail": "Revision not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RejectRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated = finance_reject_profile_revision(
                revision, actor=request.user, note=serializer.validated_data.get("note", "")
            )
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorProfileRevisionSerializer(updated).data)

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, vendor_pk=None, pk=None):
        from apps.vendors.services import reopen_profile_revision, SubmissionStateError
        vendor = self._get_vendor(request, vendor_pk)
        if not vendor:
            return Response({"detail": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "reopen profile revision"):
            return err
        revision = self._get_revision(vendor, pk)
        if not revision:
            return Response({"detail": "Revision not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RejectRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            updated = reopen_profile_revision(
                revision, actor=request.user, note=serializer.validated_data.get("note", "")
            )
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorProfileRevisionSerializer(updated).data)


    @action(detail=True, methods=["post"], url_path="apply")
    def apply(self, request, vendor_pk=None, pk=None):
        from apps.vendors.services import apply_vendor_profile_revision, SubmissionStateError
        vendor = self._get_vendor(request, vendor_pk)
        if not vendor:
            return Response({"detail": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "apply profile revision"):
            return err
        revision = self._get_revision(vendor, pk)
        if not revision:
            return Response({"detail": "Revision not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            updated = apply_vendor_profile_revision(revision, actor=request.user)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorProfileRevisionSerializer(updated).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, vendor_pk=None, pk=None):
        from apps.vendors.services import cancel_profile_revision, SubmissionStateError
        vendor = self._get_vendor(request, vendor_pk)
        if not vendor:
            return Response({"detail": "Vendor not found."}, status=status.HTTP_404_NOT_FOUND)
        if err := user_can_act_on_scope_response(request.user, vendor.scope_node_id, "cancel profile revision"):
            return err
        revision = self._get_revision(vendor, pk)
        if not revision:
            return Response({"detail": "Revision not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            updated = cancel_profile_revision(revision, actor=request.user)
        except SubmissionStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VendorProfileRevisionSerializer(updated).data)
