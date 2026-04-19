"""
Dashboard data selectors.

All queries are read-only. No business logic lives here — only data access.
"""
from django.db.models import Count, Avg, Q
from django.utils import timezone

from apps.invoices.models import Invoice, InvoiceStatus
from apps.workflow.models import (
    WorkflowInstance, WorkflowInstanceGroup, WorkflowInstanceStep,
    WorkflowInstanceBranch, WorkflowEvent, InstanceStatus, GroupStatus,
    StepStatus, AssignmentState, BranchStatus,
)
from apps.finance.models import FinanceHandoff, FinanceHandoffStatus
from apps.vendors.models import VendorOnboardingSubmission
from apps.access.selectors import get_user_visible_scope_ids, get_user_actionable_scope_ids


# ── Invoice counts by status ─────────────────────────────────────────────────

def get_invoice_status_counts(user):
    """
    Return a dict of invoice status → count for the user's visible scope nodes.
    """
    visible = get_user_visible_scope_ids(user)
    qs = Invoice.objects.filter(scope_node_id__in=visible)
    return dict(
        qs.values("status")
        .annotate(count=Count("id"))
        .values_list("status", "count")
    )


# ── Workflow instance counts ─────────────────────────────────────────────────

def get_workflow_instance_counts(user):
    """Return counts by instance status for the user's visible scope nodes."""
    visible = get_user_visible_scope_ids(user)
    qs = WorkflowInstance.objects.filter(subject_scope_node_id__in=visible)
    return dict(
        qs.values("status")
        .annotate(count=Count("id"))
        .values_list("status", "count")
    )


# ── Pending tasks count ──────────────────────────────────────────────────────

def get_pending_task_count(user):
    """Count of actionable step tasks assigned to this user."""
    return (
        WorkflowInstanceStep.objects
        .filter(
            assigned_user=user,
            status=StepStatus.WAITING,
            instance_group__status=GroupStatus.IN_PROGRESS,
            instance_group__instance__status=InstanceStatus.ACTIVE,
        )
        .count()
    )


# ── Invoices awaiting workflow attachment ────────────────────────────────────

def get_invoices_pending_workflow_count(user):
    """Invoices in PENDING_WORKFLOW status in user's visible scopes."""
    visible = get_user_visible_scope_ids(user)
    return Invoice.objects.filter(
        scope_node_id__in=visible,
        status=InvoiceStatus.PENDING_WORKFLOW,
    ).count()


# ── Invoices in active internal review ──────────────────────────────────────

def get_invoices_in_review_count(user):
    """Invoices with active workflow (IN_REVIEW / internally_approved) in user's scopes."""
    visible = get_user_visible_scope_ids(user)
    return Invoice.objects.filter(
        scope_node_id__in=visible,
        status__in=[InvoiceStatus.IN_REVIEW, InvoiceStatus.INTERNALLY_APPROVED],
    ).count()


# ── Invoices pending finance ─────────────────────────────────────────────────

def get_invoices_finance_pending_count(user):
    """Invoices in FINANCE_PENDING status in user's visible scopes."""
    visible = get_user_visible_scope_ids(user)
    return Invoice.objects.filter(
        scope_node_id__in=visible,
        status=InvoiceStatus.FINANCE_PENDING,
    ).count()


# ── Finance handoffs unresolved ───────────────────────────────────────────────

def get_finance_handoffs_unresolved_count(user):
    """Active (PENDING or SENT) finance handoffs in user's visible scopes."""
    visible = get_user_visible_scope_ids(user)
    return FinanceHandoff.objects.filter(
        scope_node_id__in=visible,
        status__in=[FinanceHandoffStatus.PENDING, FinanceHandoffStatus.SENT],
    ).count()


# ── Vendor submissions pending review ────────────────────────────────────────

def get_vendor_submissions_pending_count(user):
    """Vendor onboarding submissions awaiting marketing review in user's scopes."""
    visible = get_user_visible_scope_ids(user)
    return VendorOnboardingSubmission.objects.filter(
        invitation__scope_node_id__in=visible,
        status__in=["submitted", "marketing_pending"],
    ).count()


# ── Workflow drafts blocked (no eligible users or assignment required) ──────

def get_blocked_draft_instances(user):
    """
    Return DRAFT workflow instances that have at least one step with
    ASSIGNMENT_REQUIRED or NO_ELIGIBLE_USERS assignment state.
    """
    visible = get_user_actionable_scope_ids(user)
    blocked_steps_q = Q(
        instance_groups__instance_steps__assignment_state__in=[
            AssignmentState.ASSIGNMENT_REQUIRED,
            AssignmentState.NO_ELIGIBLE_USERS,
        ]
    )
    return list(
        WorkflowInstance.objects
        .filter(
            subject_scope_node_id__in=visible,
            status=InstanceStatus.DRAFT,
        )
        .filter(blocked_steps_q)
        .select_related("template_version", "subject_scope_node")
        .distinct()
        .values(
            "id", "subject_type", "subject_id", "status",
            "template_version__template__name",
            "subject_scope_node__name",
        )[:20]
    )


# ── SLA Stuck invoices ───────────────────────────────────────────────────────

def get_stuck_invoices(user, sla_hours=48):
    """
    Return invoices that have been in the same non-final status for > sla_hours
    and don't have an active workflow instance progressing them.
    """
    visible = get_user_visible_scope_ids(user)
    now = timezone.now()
    cutoff = now - timezone.timedelta(hours=sla_hours)

    final_statuses = [
        InvoiceStatus.FINANCE_APPROVED, InvoiceStatus.FINANCE_REJECTED,
        InvoiceStatus.REJECTED, InvoiceStatus.PAID,
    ]

    # Invoices stuck in a status without an active workflow
    stuck_statuses = [
        InvoiceStatus.PENDING_WORKFLOW, InvoiceStatus.PENDING,
        InvoiceStatus.IN_REVIEW, InvoiceStatus.INTERNALLY_APPROVED,
        InvoiceStatus.FINANCE_PENDING,
    ]

    return list(
        Invoice.objects.filter(
            scope_node_id__in=visible,
            status__in=stuck_statuses,
        )
        .exclude(
            pk__in=WorkflowInstance.objects.filter(
                status=InstanceStatus.ACTIVE,
            ).values("subject_id")
        )
        .filter(updated_at__lt=cutoff)
        .select_related("scope_node", "created_by")
        .values(
            "id", "title", "status", "amount", "currency",
            "scope_node__name", "created_by__email", "updated_at",
        )[:20]
    )


# ── Steps with assignment blockers ───────────────────────────────────────────

def get_steps_with_blockers(user):
    """
    Return active workflow steps that have ASSIGNMENT_REQUIRED or
    NO_ELIGIBLE_USERS assignment state.
    """
    visible = get_user_actionable_scope_ids(user)
    return list(
        WorkflowInstanceStep.objects
        .filter(
            instance_group__instance__subject_scope_node_id__in=visible,
            instance_group__instance__status=InstanceStatus.ACTIVE,
            instance_group__status=GroupStatus.IN_PROGRESS,
            assignment_state__in=[
                AssignmentState.ASSIGNMENT_REQUIRED,
                AssignmentState.NO_ELIGIBLE_USERS,
            ],
        )
        .select_related(
            "instance_group__instance",
            "instance_group__instance__subject_scope_node",
            "workflow_step",
            "assigned_user",
        )
        .values(
            "id", "status", "assignment_state",
            "instance_group__instance_id",
            "instance_group__instance__subject_type",
            "instance_group__instance__subject_id",
            "instance_group__instance__subject_scope_node__name",
            "workflow_step__name",
            "assigned_user__email",
        )[:20]
    )


# ── My pending tasks ─────────────────────────────────────────────────────────

def get_my_pending_tasks(user):
    """Return actionable step tasks + branch tasks for the current user."""
    step_tasks = (
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
            "workflow_step",
            "instance_group__step_group",
        )
        .order_by("instance_group__instance__created_at", "instance_group__display_order")
    )

    branch_tasks = (
        WorkflowInstanceBranch.objects
        .filter(
            assigned_user=user,
            status=BranchStatus.PENDING,
            instance__status=InstanceStatus.ACTIVE,
        )
        .select_related(
            "instance",
            "instance__subject_scope_node",
            "parent_instance_step__workflow_step",
            "target_scope_node",
        )
        .order_by("created_at")
    )

    result = []

    for st in step_tasks:
        result.append({
            "kind": "step",
            "id": st.id,
            "instance_id": st.instance_group.instance_id,
            "subject_type": st.instance_group.instance.subject_type,
            "subject_id": st.instance_group.instance.subject_id,
            "subject_scope_node": st.instance_group.instance.subject_scope_node.name,
            "group_name": st.instance_group.step_group.name,
            "step_name": st.workflow_step.name,
            "status": st.status,
            "assignment_state": st.assignment_state,
            "created_at": st.created_at,
        })

    for br in branch_tasks:
        result.append({
            "kind": "branch",
            "id": br.id,
            "instance_id": br.instance_id,
            "subject_type": br.instance.subject_type,
            "subject_id": br.instance.subject_id,
            "subject_scope_node": br.instance.subject_scope_node.name,
            "group_name": br.parent_instance_step.instance_group.step_group.name,
            "step_name": br.target_scope_node.name if br.target_scope_node else br.parent_instance_step.workflow_step.name,
            "status": br.status,
            "assignment_state": br.assignment_state,
            "created_at": br.created_at,
        })

    return result


# ── Recent approvals / rejections ────────────────────────────────────────────

def get_recent_workflow_events(user, limit=10):
    """
    Return recent workflow step approve/reject events for the user's visible scopes.
    """
    visible = get_user_visible_scope_ids(user)
    event_types = ["STEP_APPROVED", "STEP_REJECTED"]

    return list(
        WorkflowEvent.objects.filter(
            instance__subject_scope_node_id__in=visible,
            event_type__in=event_types,
        )
        .select_related(
            "instance",
            "instance__subject_scope_node",
            "actor_user",
        )
        .order_by("-created_at")[:limit]
    )


# ── Recent finance handoffs ─────────────────────────────────────────────────

def get_recent_finance_handoffs(user, limit=10):
    """Return most recent finance handoffs for the user's visible scopes."""
    visible = get_user_visible_scope_ids(user)
    return list(
        FinanceHandoff.objects
        .filter(scope_node_id__in=visible)
        .select_related("scope_node", "submitted_by")
        .order_by("-created_at")[:limit]
        .values(
            "id", "module", "subject_type", "subject_id",
            "status", "finance_reference_id", "sent_at",
            "created_at", "scope_node__name", "submitted_by__email",
        )
    )


# ── Recent vendor submissions ────────────────────────────────────────────────

def get_recent_vendor_submissions(user, limit=10):
    """Return recent vendor onboarding submissions for the user's visible scopes."""
    visible = get_user_visible_scope_ids(user)
    return list(
        VendorOnboardingSubmission.objects
        .filter(invitation__scope_node_id__in=visible)
        .select_related("invitation", "invitation__scope_node")
        .order_by("-created_at")[:limit]
        .values(
            "id", "normalized_vendor_name", "status",
            "submitted_at", "created_at",
            "invitation__scope_node__name",
        )
    )


# ── Insights: invoice status distribution ────────────────────────────────────

def get_insights_invoice_status_distribution(user):
    """Count of invoices by status for the user's visible scopes."""
    visible = get_user_visible_scope_ids(user)
    return list(
        Invoice.objects
        .filter(scope_node_id__in=visible)
        .values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )


# ── Insights: entity volume ─────────────────────────────────────────────────

def get_insights_entity_volume(user):
    """Invoice count and total amount by scope node."""
    visible = get_user_visible_scope_ids(user)
    return list(
        Invoice.objects
        .filter(scope_node_id__in=visible)
        .values("scope_node__id", "scope_node__name")
        .annotate(
            count=Count("id"),
            total_amount=Avg("amount"),
        )
        .order_by("-count")[:20]
    )


# ── Insights: finance turnaround ────────────────────────────────────────────

def get_insights_finance_turnaround(user):
    """
    Average and median time from handoff sent_at to decision acted_at.
    Only includes handoffs that have a decision.
    """
    from django.db.models.functions import Coalesce
    from django.db.models import DurationField

    visible = get_user_visible_scope_ids(user)

    handoffs = (
        FinanceHandoff.objects
        .filter(
            scope_node_id__in=visible,
            status__in=[FinanceHandoffStatus.FINANCE_APPROVED, FinanceHandoffStatus.FINANCE_REJECTED],
            sent_at__isnull=False,
        )
        .annotate(
            turnaround_hours=Coalesce(
                None,  # We compute this differently below
                0,
            )
        )
        .select_related("decisions")
    )

    results = []
    for h in handoffs:
        decision = h.decisions.order_by("acted_at").first()
        if decision and decision.acted_at and h.sent_at:
            delta = decision.acted_at - h.sent_at
            results.append({
                "handoff_id": h.id,
                "module": h.module,
                "subject_type": h.subject_type,
                "subject_id": h.subject_id,
                "decision": decision.decision,
                "turnaround_hours": round(delta.total_seconds() / 3600, 1),
                "acted_at": decision.acted_at,
            })

    return results


# ── Insights: stage turnaround ──────────────────────────────────────────────

def get_insights_stage_turnaround(user):
    """
    Average time steps spend in each workflow group/stage.
    Computed from step acted_at - group created_at for completed steps.
    """
    visible = get_user_visible_scope_ids(user)

    completed_steps = (
        WorkflowInstanceStep.objects
        .filter(
            instance_group__instance__subject_scope_node_id__in=visible,
            acted_at__isnull=False,
            instance_group__instance__status__in=[InstanceStatus.APPROVED, InstanceStatus.REJECTED],
        )
        .select_related("instance_group__step_group")
    )

    # Aggregate in Python for correctness
    from collections import defaultdict
    totals = defaultdict(lambda: {"total_hours": 0.0, "count": 0})

    for step in completed_steps:
        if step.acted_at and step.instance_group.created_at:
            delta = step.acted_at - step.instance_group.created_at
            hours = delta.total_seconds() / 3600
            group_name = step.instance_group.step_group.name
            totals[group_name]["total_hours"] += hours
            totals[group_name]["count"] += 1

    return [
        {
            "group_name": name,
            "avg_turnaround_hours": round(data["total_hours"] / data["count"], 1) if data["count"] > 0 else 0,
            "count": data["count"],
        }
        for name, data in sorted(totals.items())
    ]


# ── Insights: bottleneck stages ───────────────────────────────────────────────

def get_insights_bottleneck_stages(user, threshold_hours=24):
    """
    Identify stages where active steps have been waiting > threshold_hours.
    """
    visible = get_user_visible_scope_ids(user)
    now = timezone.now()
    cutoff = now - timezone.timedelta(hours=threshold_hours)

    return list(
        WorkflowInstanceStep.objects
        .filter(
            instance_group__instance__subject_scope_node_id__in=visible,
            instance_group__instance__status=InstanceStatus.ACTIVE,
            instance_group__status=GroupStatus.IN_PROGRESS,
            status=StepStatus.WAITING,
            created_at__lt=cutoff,
        )
        .select_related(
            "instance_group__instance",
            "instance_group__instance__subject_scope_node",
            "instance_group__step_group",
            "workflow_step",
            "assigned_user",
        )
        .values(
            "id", "created_at",
            "instance_group__step_group__name",
            "workflow_step__name",
            "assigned_user__email",
            "instance_group__instance__subject_type",
            "instance_group__instance__subject_id",
            "instance_group__instance__subject_scope_node__name",
        )
        .order_by("created_at")[:20]
    )
