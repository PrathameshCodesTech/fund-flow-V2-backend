from django.contrib import admin
from .models import (
    BudgetCategory,
    BudgetSubCategory,
    Budget,
    BudgetRule,
    BudgetConsumption,
    BudgetVarianceRequest,
)


@admin.register(BudgetCategory)
class BudgetCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "org", "is_active", "created_at")
    list_filter = ("is_active", "org")
    search_fields = ("name", "code")


@admin.register(BudgetSubCategory)
class BudgetSubCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "category", "is_active", "created_at")
    list_filter = ("is_active", "category")
    search_fields = ("name", "code")


@admin.register(Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = (
        "id", "category", "subcategory", "scope_node",
        "financial_year", "period_type", "allocated_amount",
        "reserved_amount", "consumed_amount", "currency", "status",
        "created_by", "created_at",
    )
    list_filter = ("status", "currency", "period_type", "financial_year")
    raw_id_fields = ("scope_node", "category", "subcategory", "created_by", "approved_by")


@admin.register(BudgetRule)
class BudgetRuleAdmin(admin.ModelAdmin):
    list_display = (
        "id", "budget", "warning_threshold_percent",
        "approval_threshold_percent", "hard_block_threshold_percent",
        "is_active",
    )
    list_filter = ("is_active",)


@admin.register(BudgetConsumption)
class BudgetConsumptionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "budget", "source_type", "source_id", "amount",
        "consumption_type", "status", "created_by", "created_at",
    )
    list_filter = ("consumption_type", "status", "source_type")


@admin.register(BudgetVarianceRequest)
class BudgetVarianceRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id", "budget", "source_type", "source_id",
        "requested_amount", "status",
        "requested_by", "reviewed_by", "created_at",
    )
    list_filter = ("status", "source_type")
