from apps.workflow.models import (
    WorkflowTemplate,
    WorkflowTemplateVersion,
    StepGroup,
    WorkflowStep,
    WorkflowInstance,
    WorkflowInstanceGroup,
    WorkflowInstanceStep,
    WorkflowEvent,
    VersionStatus,
    InstanceStatus,
    GroupStatus,
    StepStatus,
)


def get_published_version(template):
    return WorkflowTemplateVersion.objects.filter(
        template=template, status=VersionStatus.PUBLISHED
    ).first()


def get_templates_for_node(scope_node):
    return WorkflowTemplate.objects.filter(scope_node=scope_node).order_by("name")


def get_instances_for_subject(subject_type, subject_id):
    return WorkflowInstance.objects.filter(
        subject_type=subject_type,
        subject_id=subject_id,
    ).select_related("template_version", "subject_scope_node").order_by("-created_at")


def get_active_instances_for_node(scope_node):
    return WorkflowInstance.objects.filter(
        subject_scope_node=scope_node,
        status=InstanceStatus.ACTIVE,
    ).select_related("template_version")


def get_step_groups_for_version(version):
    return StepGroup.objects.filter(
        template_version=version
    ).prefetch_related("steps").order_by("display_order")


def get_pending_tasks_for_user(user):
    """
    Return all WorkflowInstanceStep rows that:
    - are assigned to this user
    - belong to an ACTIVE workflow instance
    - are in an IN_PROGRESS group
    - have status WAITING (actionable)
    """
    return (
        WorkflowInstanceStep.objects
        .filter(
            assigned_user=user,
            status=StepStatus.WAITING,
            instance_group__status=GroupStatus.IN_PROGRESS,
            instance_group__instance__status=InstanceStatus.ACTIVE,
        )
        .select_related(
            "instance_group__instance",
            "instance_group__instance__subject_scope_node",
            "workflow_step__group",
            "assigned_user",
        )
        .order_by(
            "instance_group__instance__created_at",
            "instance_group__display_order",
            "workflow_step__display_order",
        )
    )


def get_instance_events(instance):
    return WorkflowEvent.objects.filter(
        instance=instance
    ).select_related("actor_user", "target_user").order_by("created_at")
