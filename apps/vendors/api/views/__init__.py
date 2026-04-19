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
    Vendor,
    VendorAttachment,
    VendorFinanceActionToken,
    VendorInvitation,
    VendorOnboardingSubmission,
)
from apps.vendors.services import (
    FinanceTokenError,
    InvitationExpiredError,
    InvitationNotFoundError,
    POMandate,
    SubmissionStateError,
    VendorStateError,
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
    reopen_submission,
    send_submission_to_finance,
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
    SendToFinanceSerializer,
    VendorAttachmentCreateSerializer,
    VendorAttachmentSerializer,
    VendorInvitationCreateSerializer,
    VendorInvitationSerializer,
    VendorSerializer,
    VendorSubmissionSerializer,
    VendorUpdateSerializer,
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
        if inv_id := params.get("invitation"):
            qs = qs.filter(invitation_id=inv_id)
        if name := params.get("normalized_vendor_name"):
            qs = qs.filter(normalized_vendor_name__icontains=name)
        if email := params.get("normalized_email"):
            qs = qs.filter(normalized_email__icontains=email)
        return qs

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
                po_mandate_enabled=serializer.validated_data.get("po_mandate_enabled", False),
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
            status__in=["draft", "reopened", "submitted"]
        ).first()
        if not submission:
            return Response(
                {"detail": "No active submission found for this invitation."},
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
        from pathlib import Path
        ext = Path(file_obj.name).suffix.lower()
        if ext not in allowed_extensions:
            return Response(
                {"detail": f"File type '{ext}' is not allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attachment = attach_document(
            submission=submission,
            title=title,
            file_obj=file_obj,
            document_type=document_type,
        )
        return Response(VendorAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)


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
            status__in=["draft", "reopened"]
        ).first()
        if not submission:
            return Response(
                {"detail": "No draft or reopened submission to finalize."},
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
