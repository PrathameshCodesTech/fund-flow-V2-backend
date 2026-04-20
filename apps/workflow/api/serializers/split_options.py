from django.contrib.auth import get_user_model
from rest_framework import serializers
from apps.workflow.models import WorkflowSplitOption

User = get_user_model()


class WorkflowSplitOptionSerializer(serializers.ModelSerializer):
    entity_name = serializers.SerializerMethodField()
    approver_role_name = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    subcategory_name = serializers.SerializerMethodField()
    campaign_name = serializers.SerializerMethodField()
    budget_name = serializers.SerializerMethodField()
    allowed_approvers = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=User.objects.all(),
        required=False,
        help_text="List of user IDs explicitly allowed as approvers for this entity",
    )

    class Meta:
        model = WorkflowSplitOption
        fields = (
            "id", "workflow_step", "entity", "entity_name",
            "approver_role", "approver_role_name",
            "allowed_approvers",
            "category", "category_name",
            "subcategory", "subcategory_name",
            "campaign", "campaign_name",
            "budget", "budget_name",
            "is_active", "display_order",
        )
        read_only_fields = ("id",)

    def get_entity_name(self, obj) -> str | None:
        return obj.entity.name if obj.entity else None

    def get_approver_role_name(self, obj) -> str | None:
        return obj.approver_role.name if obj.approver_role else None

    def get_category_name(self, obj) -> str | None:
        return obj.category.name if obj.category else None

    def get_subcategory_name(self, obj) -> str | None:
        return obj.subcategory.name if obj.subcategory else None

    def get_campaign_name(self, obj) -> str | None:
        return obj.campaign.name if obj.campaign else None

    def get_budget_name(self, obj) -> str | None:
        return obj.budget.name if obj.budget else None
