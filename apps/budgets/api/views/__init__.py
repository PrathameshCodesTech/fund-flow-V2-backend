from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.views import APIView

from apps.budgets.models import (
    BudgetCategory,
    BudgetSubCategory,
    Budget,
    BudgetRule,
    BudgetConsumption,
    BudgetVarianceRequest,
    BudgetStatus,
)
from apps.budgets.api.serializers import (
    BudgetCategorySerializer,
    BudgetCategoryCreateSerializer,
    BudgetSubCategorySerializer,
    BudgetSubCategoryCreateSerializer,
    BudgetSerializer,
    BudgetCreateSerializer,
    BudgetRuleSerializer,
    BudgetRuleCreateSerializer,
    BudgetConsumptionSerializer,
    BudgetVarianceRequestSerializer,
    VarianceReviewSerializer,
    ReserveBudgetSerializer,
    ConsumeBudgetSerializer,
    ReleaseBudgetSerializer,
)
from apps.budgets.services import (
    reserve_budget,
    consume_reserved_budget,
    release_reserved_budget,
    review_variance_request,
    BudgetLimitExceeded,
    BudgetNotActiveError,
)
from apps.budgets.selectors import get_budgets_overview
from apps.access.selectors import (
    get_user_actionable_scope_ids,
    get_user_actionable_org_ids,
    get_user_visible_scope_ids,
    get_user_visible_org_ids,
)
from apps.access.services import user_can_act_on_scope_response


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------

class BudgetCategoryViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BudgetCategorySerializer

    def get_queryset(self):
        visible_org_ids = get_user_visible_org_ids(self.request.user)
        qs = BudgetCategory.objects.select_related("org").filter(org_id__in=visible_org_ids)
        org_id = self.request.query_params.get("org")
        is_active = self.request.query_params.get("is_active")
        if org_id:
            qs = qs.filter(org_id=org_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("true", "1", "yes"))
        return qs.order_by("name")

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return BudgetCategoryCreateSerializer
        return BudgetCategorySerializer

    def _check_org_actionable(self, request, org_id, action_label):
        from apps.core.models import ScopeNode
        scope_nodes = ScopeNode.objects.filter(org_id=org_id).values_list("id", flat=True)
        actionable_ids = get_user_actionable_scope_ids(request.user)
        if not any(sid in actionable_ids for sid in scope_nodes):
            return Response(
                {"detail": f"You do not have permission to {action_label} in this organisation."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def create(self, request, *args, **kwargs):
        # Enforce actionable scope: user must have direct assignment at the budget org's scope
        org_id = request.data.get("org")
        if org_id:
            if err := self._check_org_actionable(request, org_id, "create a category"):
                return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        category = self.get_object()
        if err := self._check_org_actionable(request, category.org_id, "update this category"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        category = self.get_object()
        if err := self._check_org_actionable(request, category.org_id, "update this category"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        category = self.get_object()
        if err := self._check_org_actionable(request, category.org_id, "delete this category"):
            return err
        return super().destroy(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# SubCategory
# ---------------------------------------------------------------------------

class BudgetSubCategoryViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BudgetSubCategorySerializer

    def get_queryset(self):
        visible_org_ids = get_user_visible_org_ids(self.request.user)
        qs = BudgetSubCategory.objects.select_related("category", "category__org").filter(
            category__org_id__in=visible_org_ids
        )
        category_id = self.request.query_params.get("category")
        is_active = self.request.query_params.get("is_active")
        if category_id:
            qs = qs.filter(category_id=category_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("true", "1", "yes"))
        return qs.order_by("category__name", "name")

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return BudgetSubCategoryCreateSerializer
        return BudgetSubCategorySerializer

    def _check_org_actionable(self, request, org_id, action_label):
        from apps.core.models import ScopeNode
        scope_nodes = ScopeNode.objects.filter(org_id=org_id).values_list("id", flat=True)
        actionable_ids = get_user_actionable_scope_ids(request.user)
        if not any(sid in actionable_ids for sid in scope_nodes):
            return Response(
                {"detail": f"You do not have permission to {action_label} in this organisation."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def create(self, request, *args, **kwargs):
        # Enforce actionable scope via the category's org's scope nodes
        category_id = request.data.get("category")
        if category_id:
            try:
                cat = BudgetCategory.objects.get(pk=category_id)
                if err := self._check_org_actionable(request, cat.org_id, "create a subcategory"):
                    return err
            except BudgetCategory.DoesNotExist:
                pass
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        subcategory = self.get_object()
        if err := self._check_org_actionable(request, subcategory.category.org_id, "update this subcategory"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        subcategory = self.get_object()
        if err := self._check_org_actionable(request, subcategory.category.org_id, "update this subcategory"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        subcategory = self.get_object()
        if err := self._check_org_actionable(request, subcategory.category.org_id, "delete this subcategory"):
            return err
        return super().destroy(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class BudgetViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BudgetSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = Budget.objects.select_related(
            "org", "scope_node", "category", "subcategory",
        ).prefetch_related("rule").filter(scope_node_id__in=visible_scope_ids)
        qs = qs.order_by("-created_at")

        for filter_field in ("org", "scope_node", "category", "subcategory", "status"):
            val = self.request.query_params.get(filter_field)
            if val:
                qs = qs.filter(**{filter_field: val})

        fy = self.request.query_params.get("financial_year")
        if fy:
            qs = qs.filter(financial_year=fy)

        return qs

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return BudgetCreateSerializer
        return BudgetSerializer

    def create(self, request, *args, **kwargs):
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_response(request.user, scope_node_id, "create a budget"):
                return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        budget = self.get_object()
        if err := user_can_act_on_scope_response(request.user, budget.scope_node_id, "update this budget"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        budget = self.get_object()
        if err := user_can_act_on_scope_response(request.user, budget.scope_node_id, "update this budget"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        budget = self.get_object()
        if err := user_can_act_on_scope_response(request.user, budget.scope_node_id, "delete this budget"):
            return err
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="reserve")
    def reserve(self, request, pk=None):
        """POST /budgets/{id}/reserve/ — reserve budget amount."""
        budget = self.get_object()
        # Actionable scope check: user must have direct assignment at budget's scope node
        if err := user_can_act_on_scope_response(request.user, budget.scope_node_id, "reserve budget"):
            return err
        serializer = ReserveBudgetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = reserve_budget(
                budget=budget,
                amount=data["amount"],
                source_type=data["source_type"],
                source_id=str(data["source_id"]),
                requested_by=request.user,
                note=data.get("note", ""),
            )
        except BudgetNotActiveError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except BudgetLimitExceeded as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "status": result["status"],
            "projected_utilization": str(result["projected_utilization"]),
            "current_utilization": str(result["current_utilization"]),
            "consumption": (
                BudgetConsumptionSerializer(result["consumption"]).data
                if result["consumption"]
                else None
            ),
            "variance_request": (
                BudgetVarianceRequestSerializer(result["variance_request"]).data
                if result["variance_request"]
                else None
            ),
        }, status=status.HTTP_201_CREATED if result["consumption"] else status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="consume")
    def consume(self, request, pk=None):
        """POST /budgets/{id}/consume/ — consume from reserved amount."""
        budget = self.get_object()
        # Actionable scope check
        if err := user_can_act_on_scope_response(request.user, budget.scope_node_id, "consume budget"):
            return err
        serializer = ConsumeBudgetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = consume_reserved_budget(
                budget=budget,
                amount=data["amount"],
                source_type=data["source_type"],
                source_id=str(data["source_id"]),
                consumed_by=request.user,
                note=data.get("note", ""),
            )
        except (BudgetNotActiveError, ValueError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "status": result["status"],
            "consumption": BudgetConsumptionSerializer(result["consumption"]).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, pk=None):
        """POST /budgets/{id}/release/ — release reserved amount."""
        budget = self.get_object()
        # Actionable scope check
        if err := user_can_act_on_scope_response(request.user, budget.scope_node_id, "release budget"):
            return err
        serializer = ReleaseBudgetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = release_reserved_budget(
                budget=budget,
                amount=data["amount"],
                source_type=data["source_type"],
                source_id=str(data["source_id"]),
                released_by=request.user,
                note=data.get("note", ""),
            )
        except (BudgetNotActiveError, ValueError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "status": result["status"],
            "consumption": BudgetConsumptionSerializer(result["consumption"]).data,
        }, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# BudgetRule
# ---------------------------------------------------------------------------

class BudgetRuleViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BudgetRuleSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = BudgetRule.objects.select_related("budget").filter(
            budget__scope_node_id__in=visible_scope_ids
        ).order_by("id")
        budget_id = self.request.query_params.get("budget")
        is_active = self.request.query_params.get("is_active")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() in ("true", "1", "yes"))
        return qs

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return BudgetRuleCreateSerializer
        return BudgetRuleSerializer

    def create(self, request, *args, **kwargs):
        budget_id = request.data.get("budget")
        if budget_id:
            try:
                budget = Budget.objects.get(pk=budget_id)
                if err := user_can_act_on_scope_response(request.user, budget.scope_node_id, "create a budget rule"):
                    return err
            except Budget.DoesNotExist:
                pass
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        rule = self.get_object()
        if err := user_can_act_on_scope_response(request.user, rule.budget.scope_node_id, "update this budget rule"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        rule = self.get_object()
        if err := user_can_act_on_scope_response(request.user, rule.budget.scope_node_id, "update this budget rule"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        rule = self.get_object()
        if err := user_can_act_on_scope_response(request.user, rule.budget.scope_node_id, "delete this budget rule"):
            return err
        return super().destroy(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# BudgetConsumption (read-only)
# ---------------------------------------------------------------------------

class BudgetConsumptionViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BudgetConsumptionSerializer
    http_method_names = ["get"]

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = BudgetConsumption.objects.select_related("budget", "created_by").filter(
            budget__scope_node_id__in=visible_scope_ids
        ).order_by("-created_at")
        budget_id = self.request.query_params.get("budget")
        source_type = self.request.query_params.get("source_type")
        source_id = self.request.query_params.get("source_id")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)
        if source_type:
            qs = qs.filter(source_type=source_type)
        if source_id:
            qs = qs.filter(source_id=source_id)
        return qs


# ---------------------------------------------------------------------------
# BudgetVarianceRequest
# ---------------------------------------------------------------------------

class BudgetVarianceRequestViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BudgetVarianceRequestSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = BudgetVarianceRequest.objects.select_related(
            "budget", "requested_by", "reviewed_by",
        ).filter(budget__scope_node_id__in=visible_scope_ids).order_by("-created_at")
        budget_id = self.request.query_params.get("budget")
        variance_status = self.request.query_params.get("status")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)
        if variance_status:
            qs = qs.filter(status=variance_status)
        return qs

    # Variance requests are created by reserve_budget() — prevent direct POST create
    def create(self, request, *args, **kwargs):
        return Response(
            {"detail": "Variance requests are created automatically by budget reservations."},
            status=405,
        )

    @action(detail=True, methods=["post"], url_path="review")
    def review(self, request, pk=None):
        """POST /budgets/variance-requests/{id}/review/"""
        variance_req = self.get_object()
        # Actionable scope check: user must have direct assignment at budget's scope node
        if err := user_can_act_on_scope_response(
            request.user, variance_req.budget.scope_node_id, "review variance request"
        ):
            return err
        serializer = VarianceReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            updated = review_variance_request(
                variance_request=variance_req,
                decision=data["decision"],
                reviewed_by=request.user,
                review_note=data.get("review_note", ""),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(BudgetVarianceRequestSerializer(updated).data)


# ---------------------------------------------------------------------------
# Budget Overview (analytics dashboard)
# ---------------------------------------------------------------------------

class BudgetOverviewView(APIView):
    """GET /api/v1/budgets/overview/ — aggregated budget analytics."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        payload = get_budgets_overview(request.user)
        return Response(payload)

