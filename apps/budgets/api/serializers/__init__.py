from rest_framework import serializers
from apps.budgets.models import (
    BudgetCategory,
    BudgetSubCategory,
    Budget,
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
            "id", "budget", "source_type", "source_id", "amount",
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
            "id", "budget", "budget_name", "source_type", "source_id",
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
    category_name = serializers.CharField(source="category.name", read_only=True)
    subcategory_name = serializers.CharField(
        source="subcategory.name", read_only=True, allow_null=True
    )
    scope_node_name = serializers.CharField(source="scope_node.name", read_only=True)
    has_rule = serializers.SerializerMethodField()

    class Meta:
        model = Budget
        fields = (
            "id", "org", "scope_node", "scope_node_name",
            "category", "category_name",
            "subcategory", "subcategory_name",
            "financial_year", "period_type", "period_start", "period_end",
            "allocated_amount", "reserved_amount", "consumed_amount",
            "available_amount", "utilization_percent",
            "currency", "status",
            "created_by", "approved_by", "approved_at",
            "created_at", "updated_at",
            "has_rule",
        )
        read_only_fields = (
            "id", "reserved_amount", "consumed_amount",
            "created_by", "approved_by", "approved_at",
            "created_at", "updated_at",
        )

    def get_has_rule(self, obj):
        return hasattr(obj, "rule") and obj.rule is not None


class BudgetCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Budget
        fields = (
            "org", "scope_node", "category", "subcategory",
            "financial_year", "period_type", "period_start", "period_end",
            "allocated_amount", "currency", "status",
        )

    def validate(self, data):
        if data.get("period_start") and data.get("period_end"):
            if data["period_start"] >= data["period_end"]:
                raise serializers.ValidationError({
                    "period_end": "period_end must be after period_start."
                })
        return data


# ---------------------------------------------------------------------------
# Runtime request serializers
# ---------------------------------------------------------------------------

class ReserveBudgetSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    source_type = serializers.ChoiceField(choices=SourceType.choices)
    source_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, default="", allow_blank=True)


class ConsumeBudgetSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    source_type = serializers.ChoiceField(choices=SourceType.choices)
    source_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, default="", allow_blank=True)


class ReleaseBudgetSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    source_type = serializers.ChoiceField(choices=SourceType.choices)
    source_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, default="", allow_blank=True)
