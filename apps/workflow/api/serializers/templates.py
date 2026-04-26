from django.utils.text import slugify
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
            "branch_approval_policy",
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
            "id", "name", "code", "description", "module", "scope_node",
            "is_active", "is_default", "created_by",
            "created_at", "updated_at", "versions",
        )
        read_only_fields = ("id", "created_at", "updated_at")
        # Suppress auto-generated DRF UniqueTogetherValidators from model constraints.
        # The conditional unique on is_default crashes on partial updates when is_default
        # is absent from attrs. Both uniqueness rules are enforced manually in validate().
        validators = []

    def validate(self, attrs):
        instance = self.instance

        # Auto-generate code from name only on CREATE when code is omitted.
        # On update, code is stable: changing the name must not silently mutate it.
        # Callers that want a new code must send it explicitly.
        if instance is None and not attrs.get("code") and "name" in attrs:
            attrs["code"] = slugify(attrs["name"])[:100] or "template"

        # Resolve effective values for partial-update awareness
        code = attrs.get("code", getattr(instance, "code", None))
        module = attrs.get("module", getattr(instance, "module", None))
        scope_node = attrs.get("scope_node", getattr(instance, "scope_node", None))
        is_default = attrs.get("is_default", getattr(instance, "is_default", False))

        # Validate code uniqueness per module+scope_node
        if code and module and scope_node:
            qs = WorkflowTemplate.objects.filter(
                module=module, scope_node=scope_node, code=code
            )
            if instance:
                qs = qs.exclude(pk=instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"code": f"A workflow template with code '{code}' already exists for this module and scope."}
                )

        # Validate that at most one template is the default per module+scope_node
        if is_default and module and scope_node:
            qs = WorkflowTemplate.objects.filter(
                module=module, scope_node=scope_node, is_default=True
            )
            if instance:
                qs = qs.exclude(pk=instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"is_default": "Another workflow template is already the default for this module and scope."}
                )

        return attrs
