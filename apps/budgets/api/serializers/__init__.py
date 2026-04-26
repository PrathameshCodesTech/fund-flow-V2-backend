from decimal import Decimal
from rest_framework import serializers
from apps.core.models import Organization, ScopeNode
from apps.budgets.models import (
    BudgetCategory,
    BudgetSubCategory,
    Budget,
    BudgetLine,
    BudgetRule,
    BudgetConsumption,
    BudgetVarianceRequest,
    PeriodType,
    BudgetStatus,
    ConsumptionType,
    ConsumptionStatus,
    VarianceStatus,
    SourceType,
)


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------

class BudgetCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetCategory
        fields = (
            "id", "org", "name", "code", "is_active",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class BudgetCategoryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetCategory
        fields = ("org", "name", "code")


# ---------------------------------------------------------------------------
# SubCategory
# ---------------------------------------------------------------------------

class BudgetSubCategorySerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = BudgetSubCategory
        fields = (
            "id", "category", "category_name", "name", "code", "is_active",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class BudgetSubCategoryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetSubCategory
        fields = ("category", "name", "code")


# ---------------------------------------------------------------------------
# BudgetLine
# ---------------------------------------------------------------------------

class BudgetLineSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    subcategory_name = serializers.CharField(
        source="subcategory.name", read_only=True, allow_null=True
    )
    available_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    utilization_percent = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True
    )

    class Meta:
        model = BudgetLine
        fields = (
            "id", "budget",
            "category", "category_name",
            "subcategory", "subcategory_name",
            "allocated_amount", "reserved_amount", "consumed_amount",
            "available_amount", "utilization_percent",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "budget", "reserved_amount", "consumed_amount",
            "created_at", "updated_at",
        )


class BudgetLineCreateSerializer(serializers.Serializer):
    """
    Write-side shape for a standalone BudgetLine create (POST /lines/).
    Validates: category belongs to same org as budget, subcategory belongs to category.
    """
    budget = serializers.PrimaryKeyRelatedField(queryset=Budget.objects.all())
    category = serializers.PrimaryKeyRelatedField(queryset=BudgetCategory.objects.all())
    subcategory = serializers.PrimaryKeyRelatedField(
        queryset=BudgetSubCategory.objects.all(), required=False, allow_null=True
    )
    allocated_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2,
        min_value=Decimal("0.01"),
    )

    def validate(self, data):
        budget = data["budget"]
        category = data["category"]
        subcategory = data.get("subcategory")

        # Category must belong to the same org as the budget
        if category.org_id != budget.org_id:
            raise serializers.ValidationError({
                "category": f"Category {category.id} does not belong to the same org as budget {budget.id}."
            })

        # Subcategory must belong to the selected category
        if subcategory and subcategory.category_id != category.id:
            raise serializers.ValidationError({
                "subcategory": "Subcategory does not belong to the selected category."
            })

        # Uniqueness: no duplicate (budget, category, subcategory) where subcategory is null
        # (Each category can have only one null-subcategory line per budget)
        if subcategory is None:
            if BudgetLine.objects.filter(
                budget=budget, category=category, subcategory__isnull=True
            ).exists():
                raise serializers.ValidationError({
                    "category": (
                        f"A line for category {category.id} with no subcategory already exists "
                        f"on budget {budget.id}."
                    )
                })
        else:
            if BudgetLine.objects.filter(
                budget=budget, category=category, subcategory=subcategory
            ).exists():
                raise serializers.ValidationError({
                    "subcategory": (
                        f"A line for category {category.id}, subcategory {subcategory.id} "
                        f"already exists on budget {budget.id}."
                    )
                })

        return data


class BudgetLineUpdateSerializer(serializers.Serializer):
    """
    Write-side shape for updating a standalone BudgetLine (PATCH /lines/{id}/).
    Only allocated_amount can be changed on an existing line.
    """
    category = serializers.PrimaryKeyRelatedField(
        queryset=BudgetCategory.objects.all(), required=False
    )
    subcategory = serializers.PrimaryKeyRelatedField(
        queryset=BudgetSubCategory.objects.all(), required=False, allow_null=True
    )
    allocated_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
    )

    def validate(self, data):
        category = data.get("category")
        subcategory = data.get("subcategory")

        # Cannot change category on a line that has consumed amounts
        if category is not None and subcategory is not None:
            if subcategory.category_id != category.id:
                raise serializers.ValidationError({
                    "subcategory": "Subcategory does not belong to the selected category."
                })
        return data


class BudgetLineNestedSerializer(serializers.Serializer):
    """
    Write-side shape for a budget line within a nested budget create/update payload.
    - id (optional): if present, update existing line; if omitted, create new line
    - on update: cannot change category/subcategory if reserved or consumed > 0
    """
    id = serializers.IntegerField(required=False)  # None = create new
    category = serializers.PrimaryKeyRelatedField(queryset=BudgetCategory.objects.all())
    subcategory = serializers.PrimaryKeyRelatedField(
        queryset=BudgetSubCategory.objects.all(), required=False, allow_null=True
    )
    allocated_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2,
        min_value=Decimal("0.01"),
    )

    def validate(self, data):
        category = data["category"]
        subcategory = data.get("subcategory")

        if subcategory and subcategory.category_id != category.id:
            raise serializers.ValidationError({
                "subcategory": "Subcategory does not belong to the selected category."
            })
        return data

    def validate_id(self, value):
        """Ensure id refers to a line that actually exists."""
        if value is not None:
            if not BudgetLine.objects.filter(pk=value).exists():
                raise serializers.ValidationError(
                    f"BudgetLine with id={value} does not exist."
                )
        return value


# ---------------------------------------------------------------------------
# BudgetRule
# ---------------------------------------------------------------------------

class BudgetRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetRule
        fields = (
            "id", "budget", "warning_threshold_percent",
            "approval_threshold_percent", "hard_block_threshold_percent",
            "allowed_variance_percent", "require_hod_approval_on_variance",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, data):
        warning = data.get("warning_threshold_percent", getattr(self.instance, "warning_threshold_percent", None))
        approval = data.get("approval_threshold_percent", getattr(self.instance, "approval_threshold_percent", None))
        hard_block = data.get("hard_block_threshold_percent", getattr(self.instance, "hard_block_threshold_percent", None))

        if approval is not None and warning is not None and warning >= approval:
            raise serializers.ValidationError({
                "warning_threshold_percent": "Must be less than approval_threshold_percent."
            })
        if hard_block is not None and approval is not None and approval > hard_block:
            raise serializers.ValidationError({
                "approval_threshold_percent": "Must be <= hard_block_threshold_percent."
            })
        return data


class BudgetRuleCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetRule
        fields = (
            "budget", "warning_threshold_percent", "approval_threshold_percent",
            "hard_block_threshold_percent", "allowed_variance_percent",
            "require_hod_approval_on_variance",
        )

    def validate(self, data):
        warning = data.get("warning_threshold_percent", getattr(self.instance, "warning_threshold_percent", None))
        approval = data.get("approval_threshold_percent", getattr(self.instance, "approval_threshold_percent", None))
        hard_block = data.get("hard_block_threshold_percent", getattr(self.instance, "hard_block_threshold_percent", None))

        if approval is not None and warning is not None and warning >= approval:
            raise serializers.ValidationError({
                "warning_threshold_percent": "Must be less than approval_threshold_percent."
            })
        if hard_block is not None and approval is not None and approval > hard_block:
            raise serializers.ValidationError({
                "approval_threshold_percent": "Must be <= hard_block_threshold_percent."
            })
        return data


# ---------------------------------------------------------------------------
# BudgetConsumption (read-only ledger)
# ---------------------------------------------------------------------------

class BudgetConsumptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BudgetConsumption
        fields = (
            "id", "budget", "budget_line", "source_type", "source_id", "amount",
            "consumption_type", "status", "created_by", "note", "created_at",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# BudgetVarianceRequest
# ---------------------------------------------------------------------------

class BudgetVarianceRequestSerializer(serializers.ModelSerializer):
    budget_name = serializers.CharField(source="budget.__str__", read_only=True)
    requested_by_email = serializers.CharField(source="requested_by.email", read_only=True, allow_null=True)
    reviewed_by_email = serializers.CharField(source="reviewed_by.email", read_only=True, allow_null=True)

    class Meta:
        model = BudgetVarianceRequest
        fields = (
            "id", "budget", "budget_line", "budget_name", "source_type", "source_id",
            "requested_amount", "current_utilization_percent",
            "projected_utilization_percent", "reason", "status",
            "requested_by", "requested_by_email",
            "reviewed_by", "reviewed_by_email",
            "reviewed_at", "review_note", "created_at", "updated_at",
        )
        read_only_fields = fields


class VarianceReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approved", "rejected"])
    review_note = serializers.CharField(required=False, default="", allow_blank=True)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class BudgetSerializer(serializers.ModelSerializer):
    available_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    utilization_percent = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True
    )
    scope_node_name = serializers.CharField(source="scope_node.name", read_only=True)
    has_rule = serializers.SerializerMethodField()
    lines = BudgetLineSerializer(many=True, read_only=True)

    class Meta:
        model = Budget
        fields = (
            "id", "org", "scope_node", "scope_node_name",
            "name", "code",
            "financial_year", "period_type", "period_start", "period_end",
            "allocated_amount", "reserved_amount", "consumed_amount",
            "available_amount", "utilization_percent",
            "currency", "status",
            "created_by", "approved_by", "approved_at",
            "created_at", "updated_at",
            "has_rule", "lines",
        )
        read_only_fields = (
            "id", "reserved_amount", "consumed_amount",
            "created_by", "approved_by", "approved_at",
            "created_at", "updated_at",
        )

    def get_has_rule(self, obj):
        try:
            return obj.rule is not None
        except BudgetRule.DoesNotExist:
            return False


class BudgetCreateSerializer(serializers.Serializer):
    """Write-side serializer for creating/updating a budget header + lines."""
    org = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(),
        required=False,
        allow_null=True,
    )
    scope_node = serializers.PrimaryKeyRelatedField(
        queryset=ScopeNode.objects.all(),
    )
    name = serializers.CharField(max_length=255)
    code = serializers.CharField(max_length=100)
    financial_year = serializers.CharField(max_length=20, required=False, allow_blank=True)
    period_type = serializers.ChoiceField(
        choices=PeriodType.choices, default=PeriodType.YEARLY
    )
    period_start = serializers.DateField(required=False, allow_null=True)
    period_end = serializers.DateField(required=False, allow_null=True)
    allocated_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    currency = serializers.CharField(max_length=10, default="INR")
    status = serializers.ChoiceField(choices=BudgetStatus.choices, default=BudgetStatus.DRAFT)
    lines = BudgetLineNestedSerializer(many=True, required=False)

    def validate(self, data):
        period_start = data.get("period_start")
        period_end = data.get("period_end")
        if period_start and period_end and period_start >= period_end:
            raise serializers.ValidationError({
                "period_end": "period_end must be after period_start."
            })

        lines = data.get("lines", [])
        if lines:
            lines_total = sum(line["allocated_amount"] for line in lines)
            if lines_total != data["allocated_amount"]:
                raise serializers.ValidationError({
                    "lines": (
                        f"Sum of line allocated_amounts ({lines_total}) must equal "
                        f"budget allocated_amount ({data['allocated_amount']})."
                    )
                })
        return data


class BudgetUpdateSerializer(serializers.Serializer):
    """
    Write-side serializer for updating a budget header optionally with nested line upsert.

    Header fields are all optional. Lines are also optional.

    Lines upsert logic (in the view):
        - line with `id`: update existing line
        - line without `id`: create new line
        - existing lines with `reserved_amount > 0` or `consumed_amount > 0`: cannot be deleted
        - existing lines omitted from payload: deleted (only if zero usage)
    """
    name = serializers.CharField(max_length=255, required=False)
    code = serializers.CharField(max_length=100, required=False)
    financial_year = serializers.CharField(max_length=20, required=False, allow_blank=True)
    period_type = serializers.ChoiceField(choices=PeriodType.choices, required=False)
    period_start = serializers.DateField(required=False, allow_null=True)
    period_end = serializers.DateField(required=False, allow_null=True)
    allocated_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    currency = serializers.CharField(max_length=10, required=False)
    status = serializers.ChoiceField(choices=BudgetStatus.choices, required=False)
    lines = BudgetLineNestedSerializer(many=True, required=False)

    def validate(self, data):
        period_start = data.get("period_start")
        period_end = data.get("period_end")
        if period_start and period_end and period_start >= period_end:
            raise serializers.ValidationError({
                "period_end": "period_end must be after period_start."
            })

        # If lines are provided, validate sum matches allocated_amount
        lines = data.get("lines", [])
        allocated = data.get("allocated_amount")
        if lines and allocated is not None:
            lines_total = sum(line["allocated_amount"] for line in lines)
            if lines_total != allocated:
                raise serializers.ValidationError({
                    "lines": (
                        f"Sum of line allocated_amounts ({lines_total}) must equal "
                        f"budget allocated_amount ({allocated})."
                    )
                })
        return data


# ---------------------------------------------------------------------------
# Runtime request serializers
# ---------------------------------------------------------------------------

class ReserveBudgetLineSerializer(serializers.Serializer):
    budget_line_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    source_type = serializers.ChoiceField(choices=SourceType.choices)
    source_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, default="", allow_blank=True)


class ConsumeBudgetLineSerializer(serializers.Serializer):
    budget_line_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    source_type = serializers.ChoiceField(choices=SourceType.choices)
    source_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, default="", allow_blank=True)


class ReleaseBudgetLineSerializer(serializers.Serializer):
    budget_line_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    source_type = serializers.ChoiceField(choices=SourceType.choices)
    source_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, default="", allow_blank=True)
