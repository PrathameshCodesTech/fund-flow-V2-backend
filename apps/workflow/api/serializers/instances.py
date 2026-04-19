from rest_framework import serializers
from apps.workflow.models import (
    WorkflowInstance,
    WorkflowInstanceGroup,
    WorkflowInstanceStep,
    WorkflowInstanceBranch,
)


class WorkflowInstanceBranchSerializer(serializers.ModelSerializer):
    target_scope_node_name = serializers.SerializerMethodField()
    assigned_user_email = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowInstanceBranch
        fields = (
            "id", "parent_instance_step", "instance", "target_scope_node",
            "target_scope_node_name", "branch_index", "status",
            "assigned_user", "assigned_user_email", "assignment_state",
            "acted_at", "note", "rejection_reason",
            "reassigned_from_user", "reassigned_at", "reassigned_by",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def get_target_scope_node_name(self, obj):
        return obj.target_scope_node.name if obj.target_scope_node else None

    def get_assigned_user_email(self, obj):
        return obj.assigned_user.email if obj.assigned_user else None


class WorkflowInstanceStepSerializer(serializers.ModelSerializer):
    branches = WorkflowInstanceBranchSerializer(many=True, read_only=True)
    step_kind = serializers.CharField(source="workflow_step.step_kind", read_only=True)
    step_name = serializers.CharField(source="workflow_step.name", read_only=True)
    required_role_name = serializers.CharField(source="workflow_step.required_role.name", read_only=True)

    class Meta:
        model = WorkflowInstanceStep
        fields = (
            "id", "instance_group", "workflow_step", "step_name", "step_kind",
            "required_role_name", "assigned_user",
            "assignment_state", "status", "acted_at", "note",
            "reassigned_from_user", "reassigned_at", "reassigned_by",
            "created_at", "updated_at", "branches",
        )
        read_only_fields = ("id", "created_at", "updated_at")


# ── Assignment plan serializers ──────────────────────────────────────────────

class EligibleUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()


class AssignmentPlanStepSerializer(serializers.Serializer):
    instance_step_id = serializers.IntegerField()
    workflow_step_id = serializers.IntegerField()
    step_name = serializers.CharField()
    step_kind = serializers.CharField()
    group_name = serializers.CharField()
    group_display_order = serializers.IntegerField()
    step_display_order = serializers.IntegerField()
    assigned_user = EligibleUserSerializer(allow_null=True)
    assignment_state = serializers.CharField()
    required_role = serializers.CharField()
    required_role_id = serializers.IntegerField()
    scope_resolution_policy = serializers.CharField()
    resolved_scope_node_id = serializers.IntegerField(allow_null=True)
    resolved_scope_node_name = serializers.CharField(allow_null=True)
    eligible_users = EligibleUserSerializer(many=True)


class AssignmentPlanGroupSerializer(serializers.Serializer):
    instance_group_id = serializers.IntegerField()
    step_group_id = serializers.IntegerField()
    name = serializers.CharField()
    display_order = serializers.IntegerField()
    steps = AssignmentPlanStepSerializer(many=True)


class AssignmentPlanSerializer(serializers.Serializer):
    instance_id = serializers.IntegerField()
    status = serializers.CharField()
    subject_type = serializers.CharField()
    subject_id = serializers.IntegerField()
    groups = AssignmentPlanGroupSerializer(many=True)


class WorkflowInstanceGroupSerializer(serializers.ModelSerializer):
    instance_steps = WorkflowInstanceStepSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowInstanceGroup
        fields = (
            "id", "instance", "step_group", "display_order",
            "status", "created_at", "updated_at", "instance_steps",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class WorkflowInstanceSerializer(serializers.ModelSerializer):
    instance_groups = WorkflowInstanceGroupSerializer(many=True, read_only=True)
    template_id = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowInstance
        fields = (
            "id", "template_version", "template_id",
            "subject_type", "subject_id", "subject_scope_node",
            "status", "current_group", "started_by", "started_at",
            "completed_at", "created_at", "updated_at", "instance_groups",
        )
        read_only_fields = (
            "id", "template_id", "status", "current_group",
            "started_at", "completed_at", "created_at", "updated_at",
        )

    def get_template_id(self, obj):
        return obj.template.id


class WorkflowInstanceCreateSerializer(serializers.ModelSerializer):
    """Write-only serializer used when creating a draft instance."""

    class Meta:
        model = WorkflowInstance
        fields = (
            "template_version", "subject_type", "subject_id", "subject_scope_node",
        )
