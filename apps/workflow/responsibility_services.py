from dataclasses import dataclass

from django.db import transaction

from apps.access.models import PermissionAction, PermissionResource
from apps.access.selectors import get_users_with_role_at_node
from apps.access.services import user_has_permission_including_ancestors
from apps.audit.models import AuditLog
from apps.users.models import User
from apps.workflow.models import (
    BranchStatus,
    GroupStatus,
    InstanceStatus,
    StepStatus,
    WorkflowInstanceBranch,
    WorkflowInstanceStep,
)
from apps.workflow.services import (
    StepActionError,
    get_eligible_users_for_step,
    reassign_workflow_branch,
    reassign_workflow_step,
)


class WorkflowResponsibilityError(ValueError):
    pass


class WorkflowResponsibilityPermissionError(WorkflowResponsibilityError):
    pass


@dataclass(frozen=True)
class ResponsibilitySet:
    steps: tuple
    branches: tuple

    @property
    def total(self):
        return len(self.steps) + len(self.branches)


def _pending_responsibilities(user, *, for_update=False):
    steps = WorkflowInstanceStep.objects.filter(
        assigned_user=user,
        status=StepStatus.WAITING,
        instance_group__status=GroupStatus.IN_PROGRESS,
        instance_group__instance__status=InstanceStatus.ACTIVE,
    ).select_related(
        "workflow_step__required_role",
        "workflow_step__fixed_scope_node",
        "instance_group__step_group",
        "instance_group__instance__subject_scope_node",
    ).order_by("created_at", "id")
    branches = WorkflowInstanceBranch.objects.filter(
        assigned_user=user,
        status=BranchStatus.PENDING,
        instance__status=InstanceStatus.ACTIVE,
        parent_instance_step__instance_group__status=GroupStatus.IN_PROGRESS,
    ).select_related(
        "parent_instance_step__workflow_step__required_role",
        "parent_instance_step__instance_group__step_group",
        "instance__subject_scope_node",
        "target_scope_node",
    ).order_by("created_at", "id")
    if for_update:
        steps = steps.select_for_update()
        branches = branches.select_for_update()
    return ResponsibilitySet(tuple(steps), tuple(branches))


def get_pending_workflow_responsibility_count(user):
    responsibilities = _pending_responsibilities(user)
    return {
        "steps": len(responsibilities.steps),
        "branches": len(responsibilities.branches),
        "total": responsibilities.total,
    }


def _resource_for_subject_type(subject_type):
    return {
        "invoice": PermissionResource.INVOICE,
        "campaign": PermissionResource.CAMPAIGN,
        "vendor": PermissionResource.VENDOR,
        "budget": PermissionResource.BUDGET,
    }.get(subject_type)


def _assert_actor_can_reassign(actor, responsibilities):
    for instance in [
        *(step.instance_group.instance for step in responsibilities.steps),
        *(branch.instance for branch in responsibilities.branches),
    ]:
        resource = _resource_for_subject_type(instance.subject_type)
        if resource and not user_has_permission_including_ancestors(
            actor,
            PermissionAction.REASSIGN,
            resource,
            instance.subject_scope_node,
        ):
            raise WorkflowResponsibilityPermissionError(
                f"You do not have permission to reassign {instance.subject_type} "
                f"work at {instance.subject_scope_node.name}."
            )


def _eligible_user_ids(responsibilities):
    eligible_ids = None
    for step in responsibilities.steps:
        ids = set(
            get_eligible_users_for_step(
                step.workflow_step,
                step.instance_group.instance.subject_scope_node,
            ).values_list("id", flat=True)
        )
        eligible_ids = ids if eligible_ids is None else eligible_ids & ids
    for branch in responsibilities.branches:
        ids = set(
            get_users_with_role_at_node(
                branch.parent_instance_step.workflow_step.required_role,
                branch.target_scope_node,
            ).values_list("id", flat=True)
        )
        eligible_ids = ids if eligible_ids is None else eligible_ids & ids
    return eligible_ids or set()


def _invoice_context(responsibilities):
    invoice_ids = {
        instance.subject_id
        for instance in [
            *(step.instance_group.instance for step in responsibilities.steps),
            *(branch.instance for branch in responsibilities.branches),
        ]
        if instance.subject_type == "invoice"
    }
    if not invoice_ids:
        return {}
    from apps.invoices.models import Invoice

    return {
        invoice.pk: invoice
        for invoice in Invoice.objects.filter(pk__in=invoice_ids).select_related("vendor")
    }


def _subject_data(instance, invoices):
    invoice = invoices.get(instance.subject_id) if instance.subject_type == "invoice" else None
    return {
        "subject_type": instance.subject_type,
        "subject_id": instance.subject_id,
        "subject_label": (
            invoice.vendor_invoice_number or invoice.title
            if invoice
            else f"{instance.subject_type.title()} #{instance.subject_id}"
        ),
        "vendor_name": invoice.vendor.vendor_name if invoice and invoice.vendor else None,
    }


def _user_data(user):
    return {
        "id": user.pk,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "display_name": user.get_full_name(),
    }


def get_workflow_responsibility_preview(*, target_user, actor):
    responsibilities = _pending_responsibilities(target_user)
    _assert_actor_can_reassign(actor, responsibilities)
    eligible_ids = _eligible_user_ids(responsibilities) - {target_user.pk}
    candidates = User.objects.filter(
        pk__in=eligible_ids,
        is_active=True,
    ).order_by("first_name", "last_name", "email")
    invoices = _invoice_context(responsibilities)

    items = []
    for step in responsibilities.steps:
        instance = step.instance_group.instance
        items.append({
            "task_kind": "step",
            "task_id": step.pk,
            "instance_id": instance.pk,
            "step_name": step.workflow_step.name,
            "group_name": step.instance_group.step_group.name,
            "required_role": step.workflow_step.required_role.name,
            "scope_node_id": instance.subject_scope_node_id,
            "scope_node_name": instance.subject_scope_node.name,
            **_subject_data(instance, invoices),
        })
    for branch in responsibilities.branches:
        instance = branch.instance
        items.append({
            "task_kind": "branch",
            "task_id": branch.pk,
            "instance_id": instance.pk,
            "step_name": branch.parent_instance_step.workflow_step.name,
            "group_name": branch.parent_instance_step.instance_group.step_group.name,
            "required_role": branch.parent_instance_step.workflow_step.required_role.name,
            "scope_node_id": branch.target_scope_node_id,
            "scope_node_name": branch.target_scope_node.name,
            **_subject_data(instance, invoices),
        })

    return {
        "user": _user_data(target_user),
        "counts": {
            "steps": len(responsibilities.steps),
            "branches": len(responsibilities.branches),
            "total": responsibilities.total,
        },
        "responsibilities": items,
        "eligible_replacements": [_user_data(candidate) for candidate in candidates],
    }


@transaction.atomic
def bulk_reassign_workflow_responsibilities(*, from_user, to_user, actor, reason):
    reason = (reason or "").strip()
    if not reason:
        raise WorkflowResponsibilityError("A reassignment reason is required.")
    if from_user.pk == to_user.pk:
        raise WorkflowResponsibilityError("Select a different replacement user.")
    if not to_user.is_active:
        raise WorkflowResponsibilityError("The replacement user is inactive.")

    responsibilities = _pending_responsibilities(from_user, for_update=True)
    if responsibilities.total == 0:
        raise WorkflowResponsibilityError("This user has no pending workflow responsibilities.")
    _assert_actor_can_reassign(actor, responsibilities)
    if to_user.pk not in _eligible_user_ids(responsibilities):
        raise WorkflowResponsibilityError(
            "The replacement user is not eligible for every pending responsibility."
        )

    step_ids = [step.pk for step in responsibilities.steps]
    branch_ids = [branch.pk for branch in responsibilities.branches]
    try:
        for step in responsibilities.steps:
            reassign_workflow_step(step, to_user, actor, note=reason)
        for branch in responsibilities.branches:
            reassign_workflow_branch(branch, to_user, actor, note=reason)
    except StepActionError as exc:
        raise WorkflowResponsibilityError(str(exc)) from exc

    AuditLog.objects.create(
        user=actor,
        action="workflow_responsibilities_bulk_reassigned",
        resource_type="User",
        resource_id=from_user.pk,
        metadata={
            "from_user_id": from_user.pk,
            "from_user_email": from_user.email,
            "to_user_id": to_user.pk,
            "to_user_email": to_user.email,
            "reason": reason,
            "step_ids": step_ids,
            "branch_ids": branch_ids,
        },
    )
    return {
        "steps_reassigned": len(step_ids),
        "branches_reassigned": len(branch_ids),
        "total_reassigned": len(step_ids) + len(branch_ids),
    }
