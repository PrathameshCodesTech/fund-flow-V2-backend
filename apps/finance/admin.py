from django.contrib import admin
from apps.finance.models import FinanceActionToken, FinanceDecision, FinanceHandoff


@admin.register(FinanceHandoff)
class FinanceHandoffAdmin(admin.ModelAdmin):
    list_display = ["id", "org", "scope_node", "module", "subject_type", "subject_id", "status", "sent_at"]
    list_filter = ["module", "status"]
    search_fields = ["subject_type", "subject_id", "finance_reference_id"]


@admin.register(FinanceActionToken)
class FinanceActionTokenAdmin(admin.ModelAdmin):
    list_display = ["id", "handoff", "action_type", "token", "expires_at", "used_at"]
    list_filter = ["action_type"]


@admin.register(FinanceDecision)
class FinanceDecisionAdmin(admin.ModelAdmin):
    list_display = ["id", "handoff", "decision", "reference_id", "acted_at"]
    list_filter = ["decision"]
