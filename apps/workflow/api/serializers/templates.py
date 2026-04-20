from rest_framework import serializers
from apps.workflow.models import (
    WorkflowTemplate,
    WorkflowTemplateVersion,
    StepGroup,
    WorkflowStep,
)


class WorkflowStepSerializer(serializers.ModelSerializer):
    required_role_name = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowStep
        fields = (
            "id", "group", "name", "required_role", "required_role_name",
            "scope_resolution_policy", "ancestor_node_type",
            "fixed_scope_node", "default_user", "display_order",
            "step_kind", "split_target_nodes", "split_target_mode", "join_policy",
            "allocation_total_policy", "approver_selection_mode",
            "require_category", "require_subcategory", "require_budget",
            "require_campaign", "allow_multiple_lines_per_entity",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def get_required_role_name(self, obj) -> str | None:
        return obj.required_role.name if obj.required_role else None


class StepGroupSerializer(serializers.ModelSerializer):
    steps = WorkflowStepSerializer(many=True, read_only=True)

    class Meta:
        model = StepGroup
        fields = (
            "id", "template_version", "name", "display_order",
            "parallel_mode", "on_rejection_action", "on_rejection_goto_group",
            "steps", "created_at",
        )
        read_only_fields = ("id", "created_at")


class WorkflowTemplateVersionSerializer(serializers.ModelSerializer):
    step_groups = StepGroupSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowTemplateVersion
        fields = (
            "id", "template", "version_number", "status",
            "published_at", "published_by", "created_at", "step_groups",
        )
        read_only_fields = ("id", "status", "published_at", "published_by", "created_at")


class WorkflowTemplateSerializer(serializers.ModelSerializer):
    versions = WorkflowTemplateVersionSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowTemplate
        fields = (
            "id", "name", "module", "scope_node", "created_by",
            "created_at", "updated_at", "versions",
        )
        read_only_fields = ("id", "created_at", "updated_at")
