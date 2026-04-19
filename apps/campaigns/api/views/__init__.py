from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.campaigns.models import Campaign, CampaignDocument
from apps.campaigns.api.serializers import (
    CampaignSerializer,
    CampaignCreateSerializer,
    CampaignUpdateSerializer,
    CampaignDocumentSerializer,
    CampaignDocumentCreateSerializer,
    ReviewBudgetVarianceSerializer,
    CancelCampaignSerializer,
)
from apps.campaigns.services import (
    create_campaign,
    submit_campaign_for_budget,
    review_campaign_budget_variance,
    cancel_campaign,
    CampaignStateError,
)
from apps.budgets.services import BudgetLimitExceeded, BudgetNotActiveError
from apps.access.selectors import get_user_actionable_scope_ids, get_user_visible_scope_ids
from apps.access.services import user_can_act_on_scope_response


# ---------------------------------------------------------------------------
# CampaignDocumentViewSet
# ---------------------------------------------------------------------------

class CampaignDocumentViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = CampaignDocument.objects.select_related("campaign", "uploaded_by").filter(
            campaign__scope_node_id__in=visible_scope_ids
        )
        campaign_id = self.request.query_params.get("campaign")
        if campaign_id:
            qs = qs.filter(campaign_id=campaign_id)
        return qs.order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return CampaignDocumentCreateSerializer
        return CampaignDocumentSerializer

    def perform_create(self, serializer):
        serializer.save(uploaded_by=self.request.user)

    def create(self, request, *args, **kwargs):
        # Actionable scope check via campaign's scope_node
        campaign_id = request.data.get("campaign")
        if campaign_id:
            try:
                campaign = Campaign.objects.get(pk=campaign_id)
                if err := user_can_act_on_scope_response(request.user, campaign.scope_node_id, "upload a document"):
                    return err
            except Campaign.DoesNotExist:
                pass
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        # Check actionable scope via the document's campaign
        doc = self.get_object()
        if err := user_can_act_on_scope_response(request.user, doc.campaign.scope_node_id, "delete this document"):
            return err
        return super().destroy(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# CampaignViewSet
# ---------------------------------------------------------------------------

class CampaignViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = Campaign.objects.select_related(
            "org", "scope_node", "category", "subcategory",
            "budget", "budget_variance_request", "created_by",
        ).filter(scope_node_id__in=visible_scope_ids).order_by("-created_at")

        for field in ("org", "scope_node", "status", "category", "subcategory", "budget"):
            val = self.request.query_params.get(field)
            if val:
                qs = qs.filter(**{field: val})

        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return CampaignCreateSerializer
        if self.action in ("update", "partial_update"):
            return CampaignUpdateSerializer
        return CampaignSerializer

    def perform_create(self, serializer):
        data = serializer.validated_data
        campaign = create_campaign(
            org=data.get("org"),
            scope_node=data.get("scope_node"),
            name=data["name"],
            code=data["code"],
            requested_amount=data["requested_amount"],
            created_by=self.request.user,
            description=data.get("description", ""),
            campaign_type=data.get("campaign_type", ""),
            start_date=data.get("start_date"),
            end_date=data.get("end_date"),
            currency=data.get("currency", "INR"),
            category=data.get("category"),
            subcategory=data.get("subcategory"),
            budget=data.get("budget"),
        )
        # Replace serializer instance so `get_success_headers` etc. work
        serializer.instance = campaign

    def create(self, request, *args, **kwargs):
        # Explicit actionable scope check before calling super().create()
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_response(request.user, scope_node_id, "create a campaign"):
                return err
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        out = CampaignSerializer(serializer.instance, context=self.get_serializer_context())
        headers = self.get_success_headers(out.data)
        return Response(out.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        campaign = self.get_object()
        if err := user_can_act_on_scope_response(request.user, campaign.scope_node_id, "update this campaign"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        campaign = self.get_object()
        if err := user_can_act_on_scope_response(request.user, campaign.scope_node_id, "update this campaign"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        campaign = self.get_object()
        if err := user_can_act_on_scope_response(request.user, campaign.scope_node_id, "delete this campaign"):
            return err
        return super().destroy(request, *args, **kwargs)

    # ── Actions ──────────────────────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="submit-budget")
    def submit_budget(self, request, pk=None):
        """POST /campaigns/{id}/submit-budget/ — attempt budget reservation."""
        campaign = self.get_object()
        if err := user_can_act_on_scope_response(request.user, campaign.scope_node_id, "submit budget for this campaign"):
            return err
        try:
            result = submit_campaign_for_budget(campaign, submitted_by=request.user)
        except CampaignStateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (BudgetNotActiveError, BudgetLimitExceeded, ValueError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "status": result["status"],
            "campaign": CampaignSerializer(campaign, context=self.get_serializer_context()).data,
        })

    @action(detail=True, methods=["post"], url_path="review-budget-variance")
    def review_budget_variance(self, request, pk=None):
        """POST /campaigns/{id}/review-budget-variance/"""
        campaign = self.get_object()
        if err := user_can_act_on_scope_response(request.user, campaign.scope_node_id, "review budget variance for this campaign"):
            return err
        serializer = ReviewBudgetVarianceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            review_campaign_budget_variance(
                campaign=campaign,
                decision=data["decision"],
                reviewed_by=request.user,
                review_note=data.get("review_note", ""),
            )
        except (CampaignStateError, ValueError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            CampaignSerializer(campaign, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """POST /campaigns/{id}/cancel/"""
        campaign = self.get_object()
        if err := user_can_act_on_scope_response(request.user, campaign.scope_node_id, "cancel this campaign"):
            return err
        serializer = CancelCampaignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            cancel_campaign(
                campaign=campaign,
                cancelled_by=request.user,
                note=data.get("note", ""),
            )
        except CampaignStateError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            CampaignSerializer(campaign, context=self.get_serializer_context()).data
        )
