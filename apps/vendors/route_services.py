from copy import deepcopy

from django.db import transaction
from django.db.models import Max

from apps.access.selectors import get_users_with_role_at_node
from apps.audit.models import AuditLog
from apps.users.models import User
from apps.vendors.models import VendorSubmissionRoute
from apps.workflow.models import (
    StepGroup,
    VersionStatus,
    WorkflowSplitOption,
    WorkflowStep,
    WorkflowTemplateVersion,
)
from apps.workflow.services import publish_template_version, resolve_step_target_node


class RouteAssigneeReplacementError(ValueError):
    pass


def _published_version(route):
    return (
        WorkflowTemplateVersion.objects.filter(
            template=route.workflow_template,
            status=VersionStatus.PUBLISHED,
        )
        .prefetch_related("step_groups__steps__default_user")
        .order_by("-version_number")
        .first()
    )


def _user_data(user):
    return {
        "id": user.pk,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "display_name": user.get_full_name(),
    }


def _eligible_user_ids_for_steps(steps, subject_scope_node):
    eligible_ids = None
    for step in steps:
        try:
            target_node = resolve_step_target_node(step, subject_scope_node)
        except ValueError as exc:
            raise RouteAssigneeReplacementError(str(exc)) from exc
        step_ids = set(
            get_users_with_role_at_node(step.required_role, target_node)
            .values_list("id", flat=True)
        )
        eligible_ids = step_ids if eligible_ids is None else eligible_ids & step_ids
    return eligible_ids or set()


def get_route_assignee_replacement_options(route):
    version = _published_version(route)
    if version is None:
        raise RouteAssigneeReplacementError(
            "This route has no published workflow version."
        )

    steps = list(
        WorkflowStep.objects.filter(group__template_version=version)
        .select_related("default_user", "required_role", "fixed_scope_node")
        .order_by("group__display_order", "display_order")
    )
    assignee_ids = {step.default_user_id for step in steps if step.default_user_id}
    users = {
        user.pk: user
        for user in User.objects.filter(pk__in=assignee_ids)
    }

    assignees = []
    for user_id in sorted(assignee_ids):
        affected_steps = [step for step in steps if step.default_user_id == user_id]
        candidate_ids = _eligible_user_ids_for_steps(
            affected_steps, route.workflow_template.scope_node
        )
        candidates = User.objects.filter(
            pk__in=candidate_ids,
            is_active=True,
        ).exclude(pk=user_id).order_by("first_name", "last_name", "email")
        assignees.append({
            **_user_data(users[user_id]),
            "affected_steps": [
                {
                    "id": step.pk,
                    "name": step.name,
                    "required_role": step.required_role.name,
                }
                for step in affected_steps
            ],
            "candidates": [_user_data(candidate) for candidate in candidates],
        })

    return {
        "route_id": route.pk,
        "route_code": route.code,
        "route_label": route.label,
        "published_version_id": version.pk,
        "published_version_number": version.version_number,
        "assignees": assignees,
    }


def _clone_published_version(source_version, new_version):
    group_map = {}
    source_groups = list(
        source_version.step_groups.select_related("on_rejection_goto_group")
        .prefetch_related("steps__split_options__allowed_approvers")
        .order_by("display_order")
    )

    for source_group in source_groups:
        group_map[source_group.pk] = StepGroup.objects.create(
            template_version=new_version,
            name=source_group.name,
            display_order=source_group.display_order,
            parallel_mode=source_group.parallel_mode,
            on_rejection_action=source_group.on_rejection_action,
        )

    for source_group in source_groups:
        if source_group.on_rejection_goto_group_id:
            cloned_group = group_map[source_group.pk]
            cloned_group.on_rejection_goto_group = group_map[
                source_group.on_rejection_goto_group_id
            ]
            cloned_group.save(update_fields=["on_rejection_goto_group"])

    step_map = {}
    for source_group in source_groups:
        for source_step in source_group.steps.all():
            cloned_step = WorkflowStep.objects.create(
                group=group_map[source_group.pk],
                name=source_step.name,
                required_role=source_step.required_role,
                scope_resolution_policy=source_step.scope_resolution_policy,
                ancestor_node_type=source_step.ancestor_node_type,
                fixed_scope_node=source_step.fixed_scope_node,
                default_user=source_step.default_user,
                display_order=source_step.display_order,
                step_kind=source_step.step_kind,
                split_target_nodes=deepcopy(source_step.split_target_nodes),
                split_target_mode=source_step.split_target_mode,
                join_policy=source_step.join_policy,
                allocation_total_policy=source_step.allocation_total_policy,
                approver_selection_mode=source_step.approver_selection_mode,
                require_category=source_step.require_category,
                require_subcategory=source_step.require_subcategory,
                require_budget=source_step.require_budget,
                require_campaign=source_step.require_campaign,
                allow_multiple_lines_per_entity=source_step.allow_multiple_lines_per_entity,
                branch_approval_policy=source_step.branch_approval_policy,
            )
            step_map[source_step.pk] = cloned_step

            for source_option in source_step.split_options.all():
                cloned_option = WorkflowSplitOption.objects.create(
                    workflow_step=cloned_step,
                    entity=source_option.entity,
                    approver_role=source_option.approver_role,
                    category=source_option.category,
                    subcategory=source_option.subcategory,
                    campaign=source_option.campaign,
                    budget=source_option.budget,
                    is_active=source_option.is_active,
                    display_order=source_option.display_order,
                )
                cloned_option.allowed_approvers.set(
                    source_option.allowed_approvers.all()
                )

    return step_map


@transaction.atomic
def replace_route_assignee(*, route, old_user, new_user, new_label, actor):
    route = (
        VendorSubmissionRoute.objects.select_for_update()
        .select_related("workflow_template__scope_node")
        .get(pk=route.pk)
    )
    template = route.workflow_template
    old_label = route.label
    source_version = (
        WorkflowTemplateVersion.objects.select_for_update()
        .filter(template=template, status=VersionStatus.PUBLISHED)
        .order_by("-version_number")
        .first()
    )
    if source_version is None:
        raise RouteAssigneeReplacementError(
            "This route has no published workflow version."
        )
    if old_user.pk == new_user.pk:
        raise RouteAssigneeReplacementError(
            "The replacement user must be different from the current assignee."
        )
    if not new_user.is_active:
        raise RouteAssigneeReplacementError("The replacement user is inactive.")

    affected_steps = list(
        WorkflowStep.objects.filter(
            group__template_version=source_version,
            default_user=old_user,
        ).select_related("required_role", "fixed_scope_node")
    )
    if not affected_steps:
        raise RouteAssigneeReplacementError(
            "The selected current assignee is not configured on this route's published workflow."
        )

    eligible_ids = _eligible_user_ids_for_steps(affected_steps, template.scope_node)
    if new_user.pk not in eligible_ids:
        raise RouteAssigneeReplacementError(
            "The replacement user does not hold every required role at the resolved workflow scope."
        )

    max_version = (
        WorkflowTemplateVersion.objects.filter(template=template)
        .aggregate(value=Max("version_number"))["value"]
        or 0
    )
    new_version = WorkflowTemplateVersion.objects.create(
        template=template,
        version_number=max_version + 1,
        status=VersionStatus.DRAFT,
    )
    step_map = _clone_published_version(source_version, new_version)
    cloned_affected_ids = [step_map[step.pk].pk for step in affected_steps]
    WorkflowStep.objects.filter(pk__in=cloned_affected_ids).update(
        default_user=new_user
    )

    publish_template_version(new_version, published_by=actor)
    route.label = new_label.strip()
    route.save(update_fields=["label", "updated_at"])

    AuditLog.objects.create(
        user=actor,
        action="vendor_route_assignee_replaced",
        resource_type="vendor_submission_route",
        resource_id=route.pk,
        metadata={
            "route_code": route.code,
            "old_label": old_label,
            "new_label": route.label,
            "old_user_id": old_user.pk,
            "old_user_email": old_user.email,
            "new_user_id": new_user.pk,
            "new_user_email": new_user.email,
            "source_version_id": source_version.pk,
            "source_version_number": source_version.version_number,
            "new_version_id": new_version.pk,
            "new_version_number": new_version.version_number,
            "affected_step_ids": [step.pk for step in affected_steps],
        },
    )

    return route, new_version, len(affected_steps)
