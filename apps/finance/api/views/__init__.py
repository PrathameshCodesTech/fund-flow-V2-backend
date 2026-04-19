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
    create_finance_handoff,
    finance_approve_handoff,
    finance_reject_handoff,
    get_active_handoff_for_subject,
    get_handoff_by_token,
    send_finance_handoff,
)
from apps.finance.api.serializers import (
    FinanceApproveSerializer,
    FinanceDecisionSerializer,
    FinanceHandoffSerializer,
    FinanceRejectSerializer,
    PublicFinanceTokenSerializer,
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

        # Get the specific action token for this token
        try:
            action_token = FinanceActionToken.objects.select_related("handoff").get(token=token)
        except FinanceActionToken.DoesNotExist:
            return Response({"detail": "Token not found."}, status=status.HTTP_404_NOT_FOUND)

        # Derive subject name
        if handoff.module == "invoice":
            from apps.invoices.models import Invoice
            try:
                subject_name = Invoice.objects.get(pk=handoff.subject_id).title
            except Invoice.DoesNotExist:
                subject_name = f"Invoice {handoff.subject_id}"
        elif handoff.module == "campaign":
            from apps.campaigns.models import Campaign
            try:
                subject_name = Campaign.objects.get(pk=handoff.subject_id).name
            except Campaign.DoesNotExist:
                subject_name = f"Campaign {handoff.subject_id}"
        else:
            subject_name = f"{handoff.subject_type} {handoff.subject_id}"

        data = {
            "action_type": action_token.action_type,
            "is_expired": action_token.is_expired(),
            "is_used": action_token.is_used(),
            "module": handoff.module,
            "subject_type": handoff.subject_type,
            "subject_name": subject_name,
            "handoff_status": handoff.status,
        }
        return Response(PublicFinanceTokenSerializer(data).data)


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
        try:
            handoff, decision = finance_reject_handoff(
                token_str=token,
                note=serializer.validated_data.get("note", ""),
            )
        except TokenError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except HandoffStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "handoff": FinanceHandoffSerializer(handoff).data,
            "decision": FinanceDecisionSerializer(decision).data,
        })
