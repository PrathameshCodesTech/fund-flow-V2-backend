"""
Dashboard service — assembles payloads for the ops dashboard and insights.
"""
from apps.dashboard.selectors import (
    get_invoice_status_counts,
    get_workflow_instance_counts,
    get_pending_task_count,
    get_invoices_pending_workflow_count,
    get_invoices_in_review_count,
    get_invoices_finance_pending_count,
    get_finance_handoffs_unresolved_count,
    get_vendor_submissions_pending_count,
    get_blocked_draft_instances,
    get_stuck_invoices,
    get_steps_with_blockers,
    get_my_pending_tasks,
    get_recent_workflow_events,
    get_recent_finance_handoffs,
    get_recent_vendor_submissions,
    get_insights_invoice_status_distribution,
    get_insights_entity_volume,
    get_insights_finance_turnaround,
    get_insights_stage_turnaround,
    get_insights_bottleneck_stages,
)


def get_ops_dashboard_payload(user):
    """
    Assemble the full ops dashboard payload for the given user.
    All data is fetched via selectors — this function only assembles.
    """
    invoice_status_counts = get_invoice_status_counts(user)
    workflow_counts = get_workflow_instance_counts(user)

    pending_task_count = get_pending_task_count(user)
    pending_workflow_invoices = get_invoices_pending_workflow_count(user)
    in_review_invoices = get_invoices_in_review_count(user)
    finance_pending_invoices = get_invoices_finance_pending_count(user)
    unresolved_handoffs = get_finance_handoffs_unresolved_count(user)
    vendor_pending = get_vendor_submissions_pending_count(user)
    blocked_drafts = get_blocked_draft_instances(user)
    stuck_invoices = get_stuck_invoices(user)
    blocked_steps = get_steps_with_blockers(user)
    my_tasks = get_my_pending_tasks(user)
    recent_events = get_recent_workflow_events(user, limit=10)
    recent_handoffs = get_recent_finance_handoffs(user, limit=10)
    recent_vendors = [
        {
            "id": item["id"],
            "vendor_name": item.get("normalized_vendor_name") or "Unnamed vendor",
            "status": item["status"],
            "submitted_at": item["submitted_at"],
            "created_at": item["created_at"],
            "scope_node__name": item.get("invitation__scope_node__name") or "",
        }
        for item in get_recent_vendor_submissions(user, limit=10)
    ]

    return {
        "kpis": {
            "pending_task_count": pending_task_count,
            "pending_workflow_invoices": pending_workflow_invoices,
            "in_review_invoices": in_review_invoices,
            "finance_pending_invoices": finance_pending_invoices,
            "unresolved_finance_handoffs": unresolved_handoffs,
            "vendor_submissions_pending": vendor_pending,
            "blocked_draft_instances_count": len(blocked_drafts),
        },
        "attention_queues": {
            "stuck_invoices": stuck_invoices,
            "blocked_steps": blocked_steps,
            "blocked_draft_instances": blocked_drafts,
        },
        "my_work": {
            "pending_tasks": my_tasks,
            "recent_approvals": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "actor_email": e.actor_user.email if e.actor_user else None,
                    "created_at": e.created_at,
                    "instance": {
                        "id": e.instance_id,
                        "subject_type": e.instance.subject_type,
                        "subject_id": e.instance.subject_id,
                    } if e.instance_id else None,
                }
                for e in recent_events
                if e.event_type in ("STEP_APPROVED", "STEP_REJECTED")
            ],
        },
        "recent_activity": {
            "finance_handoffs": recent_handoffs,
            "vendor_submissions": recent_vendors,
        },
        "lifecycle_summary": {
            "invoices_by_status": [
                {"status": k, "count": v}
                for k, v in sorted(invoice_status_counts.items())
            ],
            "workflow_instances_by_status": [
                {"status": k, "count": v}
                for k, v in sorted(workflow_counts.items())
            ],
        },
    }


def get_invoice_control_tower_payload(invoice, user):
    """
    Assemble the full control tower payload for a single invoice.

    Returns a denormalized payload with:
    - Invoice header summary
    - Selected workflow template/version
    - Current lifecycle phase
    - Current active group/steps
    - All workflow groups + steps with status
    - Branch summary if any
    - Workflow event timeline
    - Finance handoff summary
    - Blockers/exceptions
    """
    from apps.workflow.models import (
        WorkflowInstance, WorkflowInstanceGroup, WorkflowInstanceStep,
        WorkflowInstanceBranch, InstanceStatus, GroupStatus,
    )
    from apps.finance.models import FinanceHandoff, FinanceHandoffStatus
    from apps.access.selectors import get_user_visible_scope_ids

    # Visibility check
    visible = get_user_visible_scope_ids(user)
    if invoice.scope_node_id not in visible:
        from rest_framework.exceptions import NotAuthenticated
        raise NotAuthenticated("You do not have access to this invoice.")

    # ── Invoice header ────────────────────────────────────────────────────────
    vendor_name = None
    if invoice.vendor_id:
        vendor_name = invoice.vendor.vendor_name

    invoice_header = {
        "id": invoice.id,
        "title": invoice.title,
        "status": invoice.status,
        "amount": str(invoice.amount),
        "currency": invoice.currency,
        "po_number": invoice.po_number or None,
        "vendor_name": vendor_name,
        "scope_node_id": invoice.scope_node_id,
        "scope_node_name": invoice.scope_node.name,
        "created_by_email": invoice.created_by.email if invoice.created_by else None,
        "created_at": invoice.created_at,
        "updated_at": invoice.updated_at,
    }

    # ── Workflow template/version ─────────────────────────────────────────────
    workflow_template = None
    workflow_version = None
    if invoice.selected_workflow_template_id and invoice.selected_workflow_version_id:
        workflow_template = {
            "id": invoice.selected_workflow_template.id,
            "name": invoice.selected_workflow_template.name,
        }
        workflow_version = {
            "id": invoice.selected_workflow_version.id,
            "version_number": invoice.selected_workflow_version.version_number,
        }

    # ── Lifecycle phase ────────────────────────────────────────────────────────
    def _compute_lifecycle_phase(inv):
        if inv.status == "draft":
            return "draft"
        if inv.status == "pending_workflow":
            return "pending_workflow"
        if inv.status == "pending":
            return "awaiting_workflow_attachment"
        # Find active workflow instance
        active = WorkflowInstance.objects.filter(
            subject_type="invoice",
            subject_id=inv.pk,
            status__in=[InstanceStatus.DRAFT, InstanceStatus.ACTIVE],
        ).select_related("current_group", "current_group__step_group").first()
        if not active:
            if inv.status in ("in_review", "internally_approved"):
                return "active_internal_workflow"
            return "unknown"
        if active.status == InstanceStatus.DRAFT:
            return "draft_assignment"
        if active.current_group_id:
            return "active_internal_workflow"
        if inv.status == "internally_approved":
            return "internally_approved"
        if inv.status == "finance_pending":
            return "finance_pending"
        if inv.status == "finance_approved":
            return "finance_approved"
        if inv.status == "finance_rejected":
            return "finance_rejected"
        return "active_internal_workflow"

    lifecycle_phase = _compute_lifecycle_phase(invoice)

    # ── Active workflow instance ───────────────────────────────────────────────
    active_instance = (
        WorkflowInstance.objects
        .filter(subject_type="invoice", subject_id=invoice.pk)
        .select_related("template_version", "template_version__template", "subject_scope_node")
        .first()
    )

    current_group_info = None
    current_steps_info = []
    if active_instance and active_instance.current_group_id:
        current_group = active_instance.current_group
        current_steps = (
            current_group.instance_steps
            .select_related("workflow_step", "assigned_user")
            .order_by("workflow_step__display_order")
        )
        current_group_info = {
            "id": current_group.id,
            "name": current_group.step_group.name,
            "status": current_group.status,
            "display_order": current_group.display_order,
        }
        for step in current_steps:
            current_steps_info.append({
                "id": step.id,
                "name": step.workflow_step.name,
                "status": step.status,
                "assignment_state": step.assignment_state,
                "assigned_user_email": step.assigned_user.email if step.assigned_user else None,
                "acted_at": step.acted_at,
            })

    # ── All workflow groups + steps ──────────────────────────────────────────
    all_groups = []
    if active_instance:
        groups = (
            active_instance.instance_groups
            .select_related("step_group")
            .prefetch_related("instance_steps__workflow_step", "instance_steps__assigned_user")
            .order_by("display_order")
        )
        for group in groups:
            group_steps = []
            for step in group.instance_steps.all():
                # Get branches for this step
                branches = []
                if step.workflow_step.step_kind == "SPLIT_BY_SCOPE":
                    step_branches = (
                        step.branches
                        .select_related("target_scope_node", "assigned_user")
                        .order_by("branch_index")
                    )
                    for br in step_branches:
                        branches.append({
                            "id": br.id,
                            "target_scope_node_name": br.target_scope_node.name if br.target_scope_node else None,
                            "status": br.status,
                            "assignment_state": br.assignment_state,
                            "assigned_user_email": br.assigned_user.email if br.assigned_user else None,
                            "acted_at": br.acted_at,
                            "note": br.note,
                        })

                group_steps.append({
                    "id": step.id,
                    "name": step.workflow_step.name,
                    "status": step.status,
                    "assignment_state": step.assignment_state,
                    "step_kind": step.workflow_step.step_kind,
                    "assigned_user_email": step.assigned_user.email if step.assigned_user else None,
                    "acted_at": step.acted_at,
                    "note": step.note,
                    "branches": branches,
                })
            all_groups.append({
                "id": group.id,
                "name": group.step_group.name,
                "display_order": group.display_order,
                "status": group.status,
                "steps": group_steps,
            })

    # ── Workflow event timeline ───────────────────────────────────────────────
    timeline = []
    if active_instance:
        events = (
            active_instance.events
            .select_related("actor_user", "target_user")
            .order_by("created_at")
        )
        for ev in events:
            timeline.append({
                "id": ev.id,
                "event_type": ev.event_type,
                "actor_email": ev.actor_user.email if ev.actor_user else None,
                "target_email": ev.target_user.email if ev.target_user else None,
                "metadata": ev.metadata,
                "created_at": ev.created_at,
            })

    # ── Finance handoff ────────────────────────────────────────────────────────
    finance_handoff = (
        FinanceHandoff.objects
        .filter(module="invoice", subject_id=invoice.pk)
        .select_related("submitted_by")
        .order_by("-created_at")
        .first()
    )
    finance_info = None
    if finance_handoff:
        recipient_count = (
            finance_handoff.action_tokens.filter(action_type="approve").count()
            if finance_handoff.status != FinanceHandoffStatus.PENDING else None
        )
        finance_info = {
            "id": finance_handoff.id,
            "status": finance_handoff.status,
            "finance_reference_id": finance_handoff.finance_reference_id or None,
            "sent_at": finance_handoff.sent_at,
            "recipient_count": recipient_count,
            "created_at": finance_handoff.created_at,
        }

    # ── Blockers ────────────────────────────────────────────────────────────────
    blockers = []
    if active_instance and active_instance.status == InstanceStatus.DRAFT:
        for group in active_instance.instance_groups.all():
            for step in group.instance_steps.all():
                if step.assignment_state in ("ASSIGNMENT_REQUIRED", "NO_ELIGIBLE_USERS"):
                    blockers.append({
                        "type": "assignment_blocked",
                        "step_id": step.id,
                        "step_name": step.workflow_step.name,
                        "group_name": group.step_group.name,
                        "assignment_state": step.assignment_state,
                    })

    return {
        "invoice": invoice_header,
        "workflow_template": workflow_template,
        "workflow_version": workflow_version,
        "lifecycle_phase": lifecycle_phase,
        "active_instance": {
            "id": active_instance.id if active_instance else None,
            "status": active_instance.status if active_instance else None,
            "started_at": active_instance.started_at if active_instance else None,
            "completed_at": active_instance.completed_at if active_instance else None,
        } if active_instance else None,
        "current_group": current_group_info,
        "current_steps": current_steps_info,
        "workflow_groups": all_groups,
        "workflow_timeline": timeline,
        "finance_handoff": finance_info,
        "blockers": blockers,
    }


def get_insights_payload(user):
    """Assemble the insights payload for all analytics endpoints."""
    return {
        "invoice_status_distribution": get_insights_invoice_status_distribution(user),
        "entity_volume": get_insights_entity_volume(user),
        "finance_turnaround": get_insights_finance_turnaround(user),
        "stage_turnaround": get_insights_stage_turnaround(user),
        "bottleneck_stages": get_insights_bottleneck_stages(user),
    }
