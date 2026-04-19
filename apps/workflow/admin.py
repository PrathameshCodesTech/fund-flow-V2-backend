from django.contrib import admin
from .models import (
    WorkflowTemplate, WorkflowTemplateVersion,
    StepGroup, WorkflowStep,
    WorkflowInstance, WorkflowInstanceGroup, WorkflowInstanceStep,
    WorkflowEvent,
)


@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "module", "scope_node", "created_by", "created_at")
    list_filter = ("module",)
    search_fields = ("name",)
    raw_id_fields = ("scope_node", "created_by")


@admin.register(WorkflowTemplateVersion)
class WorkflowTemplateVersionAdmin(admin.ModelAdmin):
    list_display = ("template", "version_number", "status", "published_at", "published_by")
    list_filter = ("status",)
    raw_id_fields = ("template", "published_by")


@admin.register(StepGroup)
class StepGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "template_version", "display_order", "parallel_mode", "on_rejection_action")
    list_filter = ("parallel_mode", "on_rejection_action")
    raw_id_fields = ("template_version", "on_rejection_goto_group")


@admin.register(WorkflowStep)
class WorkflowStepAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "required_role", "scope_resolution_policy", "display_order")
    list_filter = ("scope_resolution_policy",)
    raw_id_fields = ("group", "required_role", "fixed_scope_node", "default_user")


@admin.register(WorkflowInstance)
class WorkflowInstanceAdmin(admin.ModelAdmin):
    list_display = ("id", "subject_type", "subject_id", "subject_scope_node", "status", "started_at")
    list_filter = ("status", "subject_type")
    search_fields = ("subject_id",)
    raw_id_fields = ("template_version", "subject_scope_node", "current_group", "started_by")


@admin.register(WorkflowInstanceGroup)
class WorkflowInstanceGroupAdmin(admin.ModelAdmin):
    list_display = ("id", "instance", "display_order", "status")
    list_filter = ("status",)
    raw_id_fields = ("instance", "step_group")


@admin.register(WorkflowInstanceStep)
class WorkflowInstanceStepAdmin(admin.ModelAdmin):
    list_display = ("id", "instance_group", "workflow_step", "assigned_user", "status", "acted_at")
    list_filter = ("status",)
    raw_id_fields = ("instance_group", "workflow_step", "assigned_user", "reassigned_from_user", "reassigned_by")


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(admin.ModelAdmin):
    list_display = ("id", "instance", "event_type", "actor_user", "target_user", "created_at")
    list_filter = ("event_type",)
    raw_id_fields = ("instance", "actor_user", "target_user")
