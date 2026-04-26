"""
Dashboard data selectors.

All queries are read-only. No business logic lives here — only data access.
"""
from django.db.models import Count, Avg, Q
from django.utils import timezone

from apps.invoices.models import Invoice, InvoiceStatus, InvoiceAllocation
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
    """
    Count and total amount of invoices by status.
    Returns: [{"status": "...", "label": "...", "count": N, "amount": "X.XX"}]
    """
    from django.db.models import Sum as DbSum
    from apps.invoices.models import InvoiceStatus

    visible = get_user_visible_scope_ids(user)
    STATUS_LABELS = dict(InvoiceStatus.choices)

    qs = (
        Invoice.objects
        .filter(scope_node_id__in=visible)
        .values("status")
        .annotate(count=Count("id"), total_amount=DbSum("amount"))
        .order_by("status")
    )
    return [
        {
            "status": row["status"],
            "label": STATUS_LABELS.get(row["status"], row["status"]),
            "count": row["count"],
            "amount": str(row["total_amount"] or 0),
        }
        for row in qs
    ]


# ── Insights: monthly invoice trend ────────────────────────────────────────

def get_insights_monthly_invoice_trend(user, months=12):
    """
    Invoice count and total amount by month (last N months).
    Returns: [{"month": "2026-04", "count": N, "amount": "X.XX"}]
    """
    from django.db.models import Sum as DbSum
    from django.db.models.functions import TruncMonth

    visible = get_user_visible_scope_ids(user)
    qs = (
        Invoice.objects
        .filter(scope_node_id__in=visible)
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(count=Count("id"), amount=DbSum("amount"))
        .order_by("month")
    )
    result = []
    for row in qs:
        if row["month"]:
            result.append({
                "month": row["month"].strftime("%Y-%m"),
                "count": row["count"],
                "amount": str(row["amount"] or 0),
            })
    return result[-months:]


# ── Insights: entity spend ──────────────────────────────────────────────────

def get_insights_entity_spend(user):
    """
    Spend by entity from allocations (fallback to invoice scope_node).
    Returns: [{"entity_id": N, "entity_name": "...", "amount": "X.XX", "invoice_count": N}]
    """
    from django.db.models import Sum as DbSum
    from apps.invoices.models import InvoiceAllocation

    visible = get_user_visible_scope_ids(user)

    # Try allocation-based first
    alloc_qs = (
        InvoiceAllocation.objects
        .filter(invoice__scope_node_id__in=visible)
        .select_related("entity")
        .values("entity__id", "entity__name")
        .annotate(amount=DbSum("amount"), invoice_count=Count("invoice_id", distinct=True))
        .order_by("-amount")
        [:20]
    )
    if alloc_qs:
        return [
            {
                "entity_id": row["entity__id"],
                "entity_name": row["entity__name"] or "—",
                "amount": str(row["amount"] or 0),
                "invoice_count": row["invoice_count"],
            }
            for row in alloc_qs
        ]

    # Fallback: invoice-level
    from apps.invoices.models import Invoice
    qs = (
        Invoice.objects
        .filter(scope_node_id__in=visible)
        .values("scope_node__id", "scope_node__name")
        .annotate(amount=DbSum("amount"), invoice_count=Count("id"))
        .order_by("-amount")[:20]
    )
    rows = list(qs)
    if not rows:
        from apps.budgets.models import Budget
        rows = list(
            Budget.objects
            .filter(scope_node_id__in=visible)
            .values("scope_node__id", "scope_node__name")
            .annotate(amount=DbSum("allocated_amount"), invoice_count=Count("id"))
            .order_by("-amount")[:20]
        )
    return [
        {
            "entity_id": row["scope_node__id"],
            "entity_name": row["scope_node__name"] or "—",
            "amount": str(row["amount"] or 0),
            "invoice_count": row["invoice_count"],
        }
        for row in rows
    ]


# ── Insights: category spend ────────────────────────────────────────────────

def get_insights_category_spend(user):
    """
    Spend by budget category from allocations.
    Returns: [{"category_id": N, "category_name": "...", "amount": "X.XX", "allocation_count": N}]
    """
    from django.db.models import Sum as DbSum

    visible = get_user_visible_scope_ids(user)
    qs = (
        InvoiceAllocation.objects
        .filter(invoice__scope_node_id__in=visible, category__isnull=False)
        .values("category__id", "category__name")
        .annotate(amount=DbSum("amount"), allocation_count=Count("id"))
        .order_by("-amount")[:20]
    )
    rows = list(qs)
    if not rows:
        from apps.budgets.models import BudgetLine
        rows = list(
            BudgetLine.objects
            .filter(budget__scope_node_id__in=visible, category__isnull=False)
            .values("category__id", "category__name")
            .annotate(amount=DbSum("allocated_amount"), allocation_count=Count("id"))
            .order_by("-amount")[:20]
        )
    return [
        {
            "category_id": row["category__id"],
            "category_name": row["category__name"] or "—",
            "amount": str(row["amount"] or 0),
            "allocation_count": row["allocation_count"],
        }
        for row in rows
    ]


# ── Insights: subcategory spend ────────────────────────────────────────────

def get_insights_subcategory_spend(user):
    """
    Spend by subcategory from allocations.
    Returns: [{"subcategory_id": N, "subcategory_name": "...", "category_name": "...",
               "amount": "X.XX", "allocation_count": N}]
    """
    from django.db.models import Sum as DbSum

    visible = get_user_visible_scope_ids(user)
    qs = (
        InvoiceAllocation.objects
        .filter(invoice__scope_node_id__in=visible, subcategory__isnull=False)
        .select_related("category")
        .values("subcategory__id", "subcategory__name", "category__name")
        .annotate(amount=DbSum("amount"), allocation_count=Count("id"))
        .order_by("-amount")[:20]
    )
    rows = list(qs)
    if not rows:
        from apps.budgets.models import BudgetLine
        rows = list(
            BudgetLine.objects
            .filter(budget__scope_node_id__in=visible, subcategory__isnull=False)
            .values("subcategory__id", "subcategory__name", "category__name")
            .annotate(amount=DbSum("allocated_amount"), allocation_count=Count("id"))
            .order_by("-amount")[:20]
        )
    return [
        {
            "subcategory_id": row["subcategory__id"],
            "subcategory_name": row["subcategory__name"] or "—",
            "category_name": row["category__name"] or "—",
            "amount": str(row["amount"] or 0),
            "allocation_count": row["allocation_count"],
        }
        for row in rows
    ]


# ── Insights: campaign spend ────────────────────────────────────────────────

def get_insights_campaign_spend(user):
    """
    Spend by campaign from allocations.
    Returns: [{"campaign_id": N, "campaign_name": "...", "amount": "X.XX", "allocation_count": N}]
    """
    from django.db.models import Sum as DbSum

    visible = get_user_visible_scope_ids(user)
    qs = (
        InvoiceAllocation.objects
        .filter(invoice__scope_node_id__in=visible, campaign__isnull=False)
        .values("campaign__id", "campaign__name")
        .annotate(amount=DbSum("amount"), allocation_count=Count("id"))
        .order_by("-amount")[:20]
    )
    rows = list(qs)
    if not rows:
        from apps.campaigns.models import Campaign
        rows = list(
            Campaign.objects
            .filter(scope_node_id__in=visible)
            .values("id", "name")
            .annotate(amount=DbSum("approved_amount"), allocation_count=Count("id"))
            .order_by("-amount")[:20]
        )
        return [
            {
                "campaign_id": row["id"],
                "campaign_name": row["name"] or "—",
                "amount": str(row["amount"] or 0),
                "allocation_count": row["allocation_count"],
            }
            for row in rows
        ]
    return [
        {
            "campaign_id": row["campaign__id"],
            "campaign_name": row["campaign__name"] or "—",
            "amount": str(row["amount"] or 0),
            "allocation_count": row["allocation_count"],
        }
        for row in rows
    ]


# ── Insights: budget utilization ────────────────────────────────────────────

def get_insights_budget_utilization(user):
    """
    Budget utilization from Budget model.
    Returns: [{"budget_id": N, "budget_name": "...", "allocated_amount": "X.XX",
               "consumed_amount": "X.XX", "remaining_amount": "X.XX", "utilization_percent": 72}]
    """
    from django.db.models import Sum as DbSum

    visible = get_user_visible_scope_ids(user)

    # Try to get named budgets with allocations
    qs = (
        InvoiceAllocation.objects
        .filter(invoice__scope_node_id__in=visible, budget__isnull=False)
        .select_related("budget")
        .values("budget__id", "budget__allocated_amount")
        .annotate(consumed=DbSum("amount"))
        .order_by("-consumed")[:20]
    )

    results = []
    for row in qs:
        allocated = row["budget__allocated_amount"] or 0
        consumed = row["consumed"] or 0
        try:
            util_pct = min(100, round(float(consumed) / float(allocated) * 100, 1)) if allocated else 0
        except (ValueError, ZeroDivisionError):
            util_pct = 0
        results.append({
            "budget_id": row["budget__id"],
            "budget_name": f"Budget #{row['budget__id']}",
            "allocated_amount": str(allocated),
            "consumed_amount": str(consumed),
            "remaining_amount": str(max(0, allocated - consumed)),
            "utilization_percent": util_pct,
        })

    if not results:
        from apps.budgets.models import Budget
        budget_qs = (
            Budget.objects
            .filter(scope_node_id__in=visible)
            .select_related("scope_node")
            .order_by("-allocated_amount")[:20]
        )
        for budget in budget_qs:
            allocated = budget.allocated_amount or 0
            consumed = (budget.consumed_amount or 0) + (budget.reserved_amount or 0)
            try:
                util_pct = min(200, round(float(consumed) / float(allocated) * 100, 1)) if allocated else 0
            except (ValueError, ZeroDivisionError):
                util_pct = 0
            results.append({
                "budget_id": budget.id,
                "budget_name": budget.name or (budget.scope_node.name if budget.scope_node else f"Budget #{budget.id}"),
                "allocated_amount": str(allocated),
                "consumed_amount": str(consumed),
                "remaining_amount": str(max(0, allocated - consumed)),
                "utilization_percent": util_pct,
            })

    return results


# ── Insights: workflow stage turnaround ────────────────────────────────────

def get_insights_workflow_stage_turnaround(user):
    """
    Average hours per workflow group/stage for completed workflows.
    Returns: [{"stage_name": "...", "avg_hours": 2.4, "completed_count": 8}]
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

    from collections import defaultdict
    totals: dict = defaultdict(lambda: {"total_hours": 0.0, "count": 0})

    for step in completed_steps:
        if step.acted_at and step.instance_group.created_at:
            delta = step.acted_at - step.instance_group.created_at
            hours = delta.total_seconds() / 3600
            group_name = step.instance_group.step_group.name
            totals[group_name]["total_hours"] += hours
            totals[group_name]["count"] += 1

    return [
        {
            "stage_name": name,
            "avg_hours": round(data["total_hours"] / data["count"], 1) if data["count"] > 0 else 0,
            "completed_count": data["count"],
        }
        for name, data in sorted(totals.items())
    ]


# ── Insights: finance decision turnaround ───────────────────────────────────

def get_insights_finance_turnaround(user):
    """
    Aggregate finance decision turnaround metrics.
    Returns list items + summary avg hours and count.
    """
    visible = get_user_visible_scope_ids(user)

    handoffs = (
        FinanceHandoff.objects
        .filter(
            scope_node_id__in=visible,
            status__in=[FinanceHandoffStatus.FINANCE_APPROVED, FinanceHandoffStatus.FINANCE_REJECTED],
            sent_at__isnull=False,
        )
        .prefetch_related("decisions")
    )

    results = []
    total_hours = 0.0
    count = 0
    for h in handoffs:
        decision = h.decisions.order_by("acted_at").first()
        if decision and decision.acted_at and h.sent_at:
            delta = decision.acted_at - h.sent_at
            hours = round(delta.total_seconds() / 3600, 1)
            total_hours += hours
            count += 1
            results.append({
                "handoff_id": h.id,
                "module": h.module,
                "subject_type": h.subject_type,
                "subject_id": h.subject_id,
                "decision": decision.decision,
                "turnaround_hours": hours,
                "acted_at": decision.acted_at,
            })

    avg_hours = round(total_hours / count, 1) if count > 0 else 0
    return {
        "summary": {"avg_hours": avg_hours, "completed_count": count},
        "items": results,
    }


# ── Insights: top vendors ─────────────────────────────────────────────────

def get_insights_top_vendors(user):
    """
    Top vendors by total invoice amount.
    Returns: [{"vendor_id": N, "vendor_name": "...", "invoice_count": N, "amount": "X.XX"}]
    """
    from django.db.models import Sum as DbSum

    visible = get_user_visible_scope_ids(user)
    qs = (
        Invoice.objects
        .filter(scope_node_id__in=visible, vendor__isnull=False)
        .values("vendor__id", "vendor__vendor_name")
        .annotate(amount=DbSum("amount"), invoice_count=Count("id"))
        .order_by("-amount")[:10]
    )
    return [
        {
            "vendor_id": row["vendor__id"],
            "vendor_name": row["vendor__vendor_name"] or "—",
            "invoice_count": row["invoice_count"],
            "amount": str(row["amount"] or 0),
        }
        for row in qs
    ]


# ── Insights: risk / exception alerts ─────────────────────────────────────

def get_insights_risk_alerts(user):
    """
    Operational risk/exception alerts from real data.
    """
    from django.db.models import Sum as DbSum

    visible = get_user_visible_scope_ids(user)
    now = timezone.now()
    cutoff_48h = now - timezone.timedelta(hours=48)
    alerts = []

    # 1. Finance pending > 48h
    finance_pending = Invoice.objects.filter(
        scope_node_id__in=visible,
        status=InvoiceStatus.FINANCE_PENDING,
        updated_at__lt=cutoff_48h,
    ).count()
    if finance_pending > 0:
        alerts.append({
            "severity": "warning",
            "title": "Finance pending over 2 days",
            "description": f"{finance_pending} invoice{finance_pending != 1 and 's are' or ' is'} waiting for finance decision.",
            "metric_value": str(finance_pending),
        })

    # 2. In-review stuck > 48h (active workflow but no recent movement)
    in_review_stuck = Invoice.objects.filter(
        scope_node_id__in=visible,
        status=InvoiceStatus.IN_REVIEW,
        updated_at__lt=cutoff_48h,
    ).exclude(
        pk__in=WorkflowInstance.objects.filter(status=InstanceStatus.ACTIVE).values("subject_id")
    ).count()
    if in_review_stuck > 0:
        alerts.append({
            "severity": "warning",
            "title": "In-review stalled over 2 days",
            "description": f"{in_review_stuck} invoice{in_review_stuck != 1 and 's are' or ' is'} stuck in in-review with no active workflow.",
            "metric_value": str(in_review_stuck),
        })

    # 3. Finance rejected count (recent)
    recent_rejected = Invoice.objects.filter(
        scope_node_id__in=visible,
        status=InvoiceStatus.FINANCE_REJECTED,
        updated_at__gte=cutoff_48h,
    ).count()
    if recent_rejected > 0:
        alerts.append({
            "severity": "info",
            "title": "Recent finance rejections",
            "description": f"{recent_rejected} invoice{recent_rejected != 1 and 's were' or ' was'} rejected by finance in the last 48h.",
            "metric_value": str(recent_rejected),
        })

    # 4. Budget utilization > 90%
    high_util_budgets = (
        InvoiceAllocation.objects
        .filter(invoice__scope_node_id__in=visible, budget__isnull=False)
        .values("budget__id", "budget__allocated_amount")
        .annotate(total_consumed=DbSum("amount"))
        .order_by()
    )
    for row in high_util_budgets:
        allocated = row["budget__allocated_amount"] or 0
        consumed = row["total_consumed"] or 0
        if allocated > 0:
            util_pct = float(consumed) / float(allocated) * 100
            if util_pct >= 90:
                alerts.append({
                    "severity": "critical" if util_pct >= 100 else "warning",
                    "title": f"Budget #{row['budget__id']} at {round(util_pct)}%",
                    "description": f"Consumed {round(util_pct)}% of allocated budget.",
                    "metric_value": f"{round(util_pct)}%",
                })

    # 5. Vendor concentration > 40% of total invoice value
    total_amount_qs = Invoice.objects.filter(scope_node_id__in=visible).aggregate(total=DbSum("amount"))
    total_amount = total_amount_qs["total"] or 0
    if total_amount > 0:
        vendor_amounts = (
            Invoice.objects
            .filter(scope_node_id__in=visible, vendor__isnull=False)
            .values("vendor__id", "vendor__vendor_name")
            .annotate(amount=DbSum("amount"))
            .order_by("-amount")
        )
        for row in vendor_amounts[:3]:
            if row["amount"] and float(row["amount"]) > float(total_amount) * 0.4:
                alerts.append({
                    "severity": "warning",
                    "title": f"Vendor concentration: {row['vendor__vendor_name']}",
                    "description": f"This vendor represents {round(float(row['amount']) / float(total_amount) * 100)}% of total invoice value.",
                    "metric_value": f"{round(float(row['amount']) / float(total_amount) * 100)}%",
                })
                break  # Only flag the top one

    return alerts


# ── Insights: entity volume (legacy) ───────────────────────────────────────

def get_insights_entity_volume(user):
    """Invoice count and total amount by scope node (legacy)."""
    from django.db.models import Sum as DbSum

    visible = get_user_visible_scope_ids(user)
    return list(
        Invoice.objects
        .filter(scope_node_id__in=visible)
        .values("scope_node__id", "scope_node__name")
        .annotate(
            count=Count("id"),
            total_amount=DbSum("amount"),
        )
        .order_by("-count")[:20]
    )


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
