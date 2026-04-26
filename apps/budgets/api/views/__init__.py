from django.db import transaction
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.views import APIView

from apps.budgets.models import (
    BudgetCategory,
    BudgetSubCategory,
    Budget,
    BudgetLine,
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
    BudgetUpdateSerializer,
    BudgetLineSerializer,
    BudgetLineCreateSerializer,
    BudgetLineUpdateSerializer,
    BudgetRuleSerializer,
    BudgetRuleCreateSerializer,
    BudgetConsumptionSerializer,
    BudgetVarianceRequestSerializer,
    VarianceReviewSerializer,
    ReserveBudgetLineSerializer,
    ConsumeBudgetLineSerializer,
    ReleaseBudgetLineSerializer,
)
from apps.budgets.services import (
    reserve_budget_line,
    consume_reserved_budget_line,
    release_reserved_budget_line,
    review_variance_request,
    resolve_budget_line_for_allocation,
    BudgetLineNotFoundError,
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
from apps.access.services import user_can_act_on_scope_or_ancestors_response


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
            "org", "scope_node",
        ).prefetch_related("rule", "lines__category", "lines__subcategory").filter(
            scope_node_id__in=visible_scope_ids
        )
        qs = qs.order_by("-created_at")

        for filter_field in ("org", "scope_node", "status"):
            val = self.request.query_params.get(filter_field)
            if val:
                qs = qs.filter(**{filter_field: val})

        fy = self.request.query_params.get("financial_year")
        if fy:
            qs = qs.filter(financial_year=fy)

        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return BudgetCreateSerializer
        if self.action in ("update", "partial_update"):
            return BudgetUpdateSerializer
        return BudgetSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_or_ancestors_response(request.user, scope_node_id, "create a budget"):
                return err

        serializer = BudgetCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        lines_data = data.pop("lines", [])

        budget = Budget.objects.create(
            created_by=request.user,
            **data,
        )

        for line_data in lines_data:
            BudgetLine.objects.create(
                budget=budget,
                category=line_data["category"],
                subcategory=line_data.get("subcategory"),
                allocated_amount=line_data["allocated_amount"],
            )

        output = BudgetSerializer(budget, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        budget = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, budget.scope_node_id, "update this budget"):
            return err

        partial = kwargs.pop("partial", False)
        serializer = BudgetUpdateSerializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        lines_data = data.pop("lines", None)

        # Update header fields
        for field, value in data.items():
            setattr(budget, field, value)
        budget.save()

        # Nested line upsert
        if lines_data is not None:
            # IDs present in payload
            payload_line_ids = {l["id"] for l in lines_data if "id" in l}

            # Delete lines omitted from payload (only if zero usage)
            for existing_line in budget.lines.all():
                if existing_line.id not in payload_line_ids:
                    if existing_line.reserved_amount > 0 or existing_line.consumed_amount > 0:
                        raise ValidationError(
                            f"Cannot remove line {existing_line.id}: it has "
                            f"reserved={existing_line.reserved_amount}, "
                            f"consumed={existing_line.consumed_amount}. "
                            "Release or consume all amounts first."
                        )
                    existing_line.delete()

            # Upsert lines from payload
            for line_data in lines_data:
                line_id = line_data.pop("id", None)
                if line_id:
                    # Update existing line
                    line = BudgetLine.objects.get(pk=line_id, budget=budget)
                    # Category/subcategory change not allowed once the line has usage,
                    # but unchanged values are fine and should not block normal edits.
                    incoming_category = line_data.get("category", line.category)
                    incoming_subcategory = line_data.get("subcategory", line.subcategory)
                    category_changed = incoming_category.id != line.category_id
                    subcategory_changed = (
                        (incoming_subcategory.id if incoming_subcategory else None)
                        != line.subcategory_id
                    )
                    if (category_changed or subcategory_changed) and (
                        line.reserved_amount > 0 or line.consumed_amount > 0
                    ):
                        raise ValidationError(
                            f"Cannot change category/subcategory on line {line_id}: "
                            "line has existing reservations or consumption."
                        )
                    for field, value in line_data.items():
                        setattr(line, field, value)
                    line.save()
                else:
                    # Create new line
                    BudgetLine.objects.create(
                        budget=budget,
                        category=line_data["category"],
                        subcategory=line_data.get("subcategory"),
                        allocated_amount=line_data["allocated_amount"],
                    )

        output = BudgetSerializer(budget, context={"request": request})
        return Response(output.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        budget = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, budget.scope_node_id, "delete this budget"):
            return err
        return super().destroy(request, *args, **kwargs)

    def _resolve_line(self, budget: Budget, budget_line_id: int):
        """Look up a BudgetLine that belongs to this budget."""
        try:
            return BudgetLine.objects.get(pk=budget_line_id, budget=budget)
        except BudgetLine.DoesNotExist:
            return None

    @action(detail=True, methods=["post"], url_path="reserve")
    def reserve(self, request, pk=None):
        """POST /budgets/{id}/reserve/ — reserve amount against a budget line."""
        budget = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, budget.scope_node_id, "reserve budget"):
            return err

        serializer = ReserveBudgetLineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        line = self._resolve_line(budget, data["budget_line_id"])
        if line is None:
            return Response(
                {"detail": f"Budget line {data['budget_line_id']} not found on this budget."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = reserve_budget_line(
                line=line,
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
        """POST /budgets/{id}/consume/ — consume from reserved amount on a budget line."""
        budget = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, budget.scope_node_id, "consume budget"):
            return err

        serializer = ConsumeBudgetLineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        line = self._resolve_line(budget, data["budget_line_id"])
        if line is None:
            return Response(
                {"detail": f"Budget line {data['budget_line_id']} not found on this budget."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = consume_reserved_budget_line(
                line=line,
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
        """POST /budgets/{id}/release/ — release reserved amount on a budget line."""
        budget = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, budget.scope_node_id, "release budget"):
            return err

        serializer = ReleaseBudgetLineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        line = self._resolve_line(budget, data["budget_line_id"])
        if line is None:
            return Response(
                {"detail": f"Budget line {data['budget_line_id']} not found on this budget."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = release_reserved_budget_line(
                line=line,
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
# BudgetLine (standalone CRUD)
# ---------------------------------------------------------------------------

class BudgetLineViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BudgetLineSerializer
    http_method_names = ["get", "post", "patch", "delete"]

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = BudgetLine.objects.select_related(
            "budget", "budget__scope_node", "category", "subcategory"
        ).filter(budget__scope_node_id__in=visible_scope_ids)

        budget_id = self.request.query_params.get("budget")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)
        category_id = self.request.query_params.get("category")
        if category_id:
            qs = qs.filter(category_id=category_id)
        return qs.order_by("budget_id", "category__name")

    def get_serializer_class(self):
        if self.action == "create":
            return BudgetLineCreateSerializer
        if self.action in ("update", "partial_update"):
            return BudgetLineUpdateSerializer
        return BudgetLineSerializer

    def create(self, request, *args, **kwargs):
        budget_id = request.data.get("budget")
        if budget_id:
            try:
                budget = Budget.objects.get(pk=budget_id)
                if err := user_can_act_on_scope_or_ancestors_response(request.user, budget.scope_node_id, "add a budget line"):
                    return err
            except Budget.DoesNotExist:
                pass

        serializer = BudgetLineCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        line = BudgetLine.objects.create(
            budget=data["budget"],
            category=data["category"],
            subcategory=data.get("subcategory"),
            allocated_amount=data["allocated_amount"],
        )
        output = BudgetLineSerializer(line, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        line = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, line.budget.scope_node_id, "update this budget line"):
            return err

        partial = kwargs.pop("partial", False)
        serializer = BudgetLineUpdateSerializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        for field, value in data.items():
            setattr(line, field, value)
        line.save()

        output = BudgetLineSerializer(line, context={"request": request})
        return Response(output.data)

    def partial_update(self, request, *args, **kwargs):
        line = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, line.budget.scope_node_id, "update this budget line"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        line = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, line.budget.scope_node_id, "delete this budget line"):
            return err
        return super().destroy(request, *args, **kwargs)


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
                if err := user_can_act_on_scope_or_ancestors_response(request.user, budget.scope_node_id, "create a budget rule"):
                    return err
            except Budget.DoesNotExist:
                pass
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        rule = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, rule.budget.scope_node_id, "update this budget rule"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        rule = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, rule.budget.scope_node_id, "update this budget rule"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        rule = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(request.user, rule.budget.scope_node_id, "delete this budget rule"):
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
        qs = BudgetConsumption.objects.select_related("budget", "budget_line", "created_by").filter(
            budget__scope_node_id__in=visible_scope_ids
        ).order_by("-created_at")
        budget_id = self.request.query_params.get("budget")
        budget_line_id = self.request.query_params.get("budget_line")
        source_type = self.request.query_params.get("source_type")
        source_id = self.request.query_params.get("source_id")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)
        if budget_line_id:
            qs = qs.filter(budget_line_id=budget_line_id)
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
            "budget", "budget_line", "requested_by", "reviewed_by",
        ).filter(budget__scope_node_id__in=visible_scope_ids).order_by("-created_at")
        budget_id = self.request.query_params.get("budget")
        variance_status = self.request.query_params.get("status")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)
        if variance_status:
            qs = qs.filter(status=variance_status)
        return qs

    def create(self, request, *args, **kwargs):
        return Response(
            {"detail": "Variance requests are created automatically by budget reservations."},
            status=405,
        )

    @action(detail=True, methods=["post"], url_path="review")
    def review(self, request, pk=None):
        """POST /budgets/variance-requests/{id}/review/"""
        variance_req = self.get_object()
        if err := user_can_act_on_scope_or_ancestors_response(
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
