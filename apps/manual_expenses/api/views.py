from rest_framework import status, viewsets, mixins
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.exceptions import PermissionDenied, ValidationError

from django.db.models import Q

from apps.manual_expenses.models import ManualExpenseEntry, ManualExpenseAttachment, ExpenseStatus
from apps.manual_expenses.api.serializers import (
    ManualExpenseListSerializer,
    ManualExpenseDetailSerializer,
    ManualExpenseCreateSerializer,
    ManualExpenseAttachmentSerializer,
    SubmitExpenseSerializer,
    SettleExpenseSerializer,
    CancelExpenseSerializer,
)
from apps.manual_expenses import services
from apps.access.selectors import get_user_visible_scope_ids, get_user_actionable_scope_ids


class ManualExpenseViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["actionable_scope_ids"] = get_user_actionable_scope_ids(self.request.user)
        return context

    def get_serializer_class(self):
        if self.action == "list":
            return ManualExpenseListSerializer
        if self.action in ("create", "update", "partial_update"):
            return ManualExpenseCreateSerializer
        if self.action in ("submit", "settle", "cancel"):
            return None
        return ManualExpenseDetailSerializer

    def get_queryset(self):
        qs = ManualExpenseEntry.objects.select_related(
            "org", "scope_node", "created_by",
            "budget", "category", "subcategory", "vendor",
        ).order_by("-created_at")

        # Scope filter
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = qs.filter(scope_node_id__in=visible_scope_ids)

        # Filters
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        payment_method = self.request.query_params.get("payment_method")
        if payment_method:
            qs = qs.filter(payment_method=payment_method)

        scope_node = self.request.query_params.get("scope_node")
        if scope_node:
            qs = qs.filter(scope_node_id=scope_node)

        budget_id = self.request.query_params.get("budget")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)

        date_from = self.request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(expense_date__gte=date_from)

        date_to = self.request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(expense_date__lte=date_to)

        return qs

    def perform_create(self, serializer):
        actionable_ids = get_user_actionable_scope_ids(self.request.user)
        if not actionable_ids:
            raise PermissionDenied("You do not have permission to create manual expenses.")

        scope_node = serializer.validated_data.get("scope_node")
        if scope_node is None:
            if len(actionable_ids) != 1:
                raise ValidationError({"scope_node": "Scope node is required when you have access to multiple scopes."})
            from apps.core.models import ScopeNode
            scope_node = ScopeNode.objects.only("id", "org_id").get(pk=actionable_ids[0])

        if scope_node.id not in actionable_ids:
            raise PermissionDenied("You do not have permission to create manual expenses at this scope.")

        org = scope_node.org

        serializer.save(
            org=org,
            scope_node=scope_node,
            created_by=self.request.user,
        )

    def update(self, request, *args, **kwargs):
        expense = self.get_object()
        if expense.status != ExpenseStatus.DRAFT:
            return Response(
                {"detail": "Only DRAFT expenses can be edited."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if expense.created_by_id != request.user.id:
            return Response(
                {"detail": "Only the creator can edit this expense."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(expense, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        expense = self.get_object()
        if expense.status != ExpenseStatus.DRAFT:
            return Response(
                {"detail": "Only DRAFT expenses can be edited."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if expense.created_by_id != request.user.id:
            return Response(
                {"detail": "Only the creator can edit this expense."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(expense, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        expense = self.get_object()
        if expense.created_by_id != request.user.id:
            return Response(
                {"detail": "Only the creator can submit this expense."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            expense = services.submit_expense(expense)
        except services.ExpenseValidationError as e:
            if isinstance(e.args[0], dict):
                return Response(e.args[0], status=status.HTTP_400_BAD_REQUEST)
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        serializer = ManualExpenseDetailSerializer(expense, context={"request": request})
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def settle(self, request, pk=None):
        expense = self.get_object()
        actionable_scope_ids = get_user_actionable_scope_ids(request.user)
        if expense.scope_node_id not in actionable_scope_ids:
            return Response(
                {"detail": "You do not have permission to settle this expense."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            expense = services.mark_expense_settled(expense)
        except services.ExpenseValidationError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        serializer = ManualExpenseDetailSerializer(expense, context={"request": request})
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        expense = self.get_object()
        actionable_scope_ids = get_user_actionable_scope_ids(request.user)
        can_cancel = (
            expense.status == ExpenseStatus.DRAFT and expense.created_by_id == request.user.id
        ) or expense.scope_node_id in actionable_scope_ids
        if not can_cancel:
            return Response(
                {"detail": "You do not have permission to cancel this expense."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            expense = services.cancel_expense(expense)
        except services.ExpenseValidationError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        serializer = ManualExpenseDetailSerializer(expense, context={"request": request})
        return Response(serializer.data)


class ManualExpenseAttachmentViewSet(
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    POST   / — upload attachment for an expense
    DELETE /{id} — remove attachment (only on DRAFT/SUBMITTED)
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ManualExpenseAttachmentSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        return ManualExpenseAttachment.objects.filter(
            expense_entry__scope_node_id__in=visible_scope_ids
        ).select_related("expense_entry")

    def create(self, request, *args, **kwargs):
        expense_id = request.data.get("expense_entry")
        if not expense_id:
            return Response(
                {"detail": "expense_entry is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        visible_scope_ids = get_user_visible_scope_ids(
            request.user, "manual_expenses", "write"
        )
        try:
            expense = ManualExpenseEntry.objects.get(
                pk=expense_id,
                scope_node_id__in=visible_scope_ids,
            )
        except ManualExpenseEntry.DoesNotExist:
            return Response(
                {"detail": "Expense not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        file = request.FILES.get("file")
        title = request.data.get("title", "")
        document_type = request.data.get("document_type", "")

        if not file:
            return Response(
                {"detail": "file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            attachment = services.upload_expense_attachment(
                expense=expense,
                file=file,
                title=title,
                document_type=document_type,
                uploaded_by=request.user,
            )
        except services.ExpenseValidationError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ManualExpenseAttachmentSerializer(attachment, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        attachment = self.get_object()
        expense = attachment.expense_entry
        actionable_scope_ids = get_user_actionable_scope_ids(request.user)
        can_delete = (
            expense.status == ExpenseStatus.DRAFT and expense.created_by_id == request.user.id
        ) or expense.scope_node_id in actionable_scope_ids
        if not can_delete:
            return Response(
                {"detail": "You do not have permission to delete this attachment."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            services.delete_expense_attachment(attachment)
        except services.ExpenseValidationError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)
