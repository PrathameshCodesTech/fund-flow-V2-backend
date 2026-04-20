"""
Runtime split allocation service.

Handles RUNTIME_SPLIT_ALLOCATION step kind:
  - get_runtime_split_options: returns allowed entities, eligible approvers,
    existing draft/correction allocations, invoice summary.
  - submit_runtime_invoice_split: validates, creates InvoiceAllocation rows,
    reserves budget, creates branch tasks.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.access.selectors import get_users_with_role_at_node
from apps.workflow.models import (
    AssignmentState,
    BranchStatus,
    GroupStatus,
    StepKind,
    StepStatus,
    WorkflowEventType,
    WorkflowInstance,
    WorkflowInstanceBranch,
    WorkflowInstanceStep,
)
from apps.workflow.services import StepActionError, _emit_event


# ---------------------------------------------------------------------------
# get_runtime_split_options
# ---------------------------------------------------------------------------

def get_runtime_split_options(instance_step: WorkflowInstanceStep, user) -> dict:
    """
    Return all the data the frontend needs to render the split allocation form:
      - invoice summary (amount, currency, title, vendor)
      - allowed entities from WorkflowSplitOption config
      - eligible approvers per entity
      - existing draft/correction allocations for this step (if any)
    """
    from apps.workflow.models import WorkflowSplitOption
    from apps.invoices.models import Invoice, InvoiceAllocation
    from apps.budgets.models import Budget, BudgetCategory, BudgetStatus
    from apps.campaigns.models import Campaign, CampaignStatus

    step = instance_step.workflow_step
    instance = instance_step.instance_group.instance

    if step.step_kind != StepKind.RUNTIME_SPLIT_ALLOCATION:
        raise StepActionError(
            f"Step {step.id} is not a RUNTIME_SPLIT_ALLOCATION step (kind={step.step_kind})."
        )

    # Invoice subject
    if instance.subject_type != "invoice":
        raise StepActionError("Runtime split allocation is only supported for invoice subjects.")

    try:
        invoice = Invoice.objects.select_related("vendor", "scope_node").get(pk=instance.subject_id)
    except Invoice.DoesNotExist:
        raise StepActionError(f"Invoice {instance.subject_id} not found.")

    invoice_summary = {
        "id": invoice.id,
        "title": invoice.title,
        "amount": str(invoice.amount),
        "currency": invoice.currency,
        "vendor_name": invoice.vendor.vendor_name if invoice.vendor else None,
        "scope_node_id": invoice.scope_node_id,
        "scope_node_name": invoice.scope_node.name if invoice.scope_node else None,
    }

    # Split options config
    split_options_qs = WorkflowSplitOption.objects.filter(
        workflow_step=step, is_active=True
    ).select_related(
        "entity", "approver_role", "category", "subcategory", "campaign", "budget"
    ).prefetch_related("allowed_approvers").order_by("display_order")

    allowed_entities = []
    for opt in split_options_qs:
        target_path = opt.entity.path
        target_scope_filter = {
            "scope_node__org_id": opt.entity.org_id,
            "scope_node__path__startswith": target_path,
        }

        category_ids = set()
        subcategories = []
        seen_subcategory_ids = set()
        scoped_budgets = (
            Budget.objects.filter(**target_scope_filter)
            .select_related("scope_node", "category", "subcategory")
            .order_by("scope_node__name", "category__name", "subcategory__name")
        )
        for b in scoped_budgets:
            if b.category_id:
                category_ids.add(b.category_id)
            if not b.subcategory_id or not b.subcategory or not b.subcategory.is_active:
                continue
            if b.subcategory_id in seen_subcategory_ids:
                continue
            seen_subcategory_ids.add(b.subcategory_id)
            subcategories.append({
                "id": b.subcategory_id,
                "name": b.subcategory.name,
                "category_id": b.category_id,
                "category_name": b.category.name if b.category else None,
            })

        campaigns = []
        for c in (
            Campaign.objects.filter(
                **target_scope_filter,
                status__in=[
                    CampaignStatus.INTERNALLY_APPROVED,
                    CampaignStatus.FINANCE_PENDING,
                    CampaignStatus.FINANCE_APPROVED,
                ],
            )
            .select_related("category", "subcategory", "budget")
            .order_by("name")
        ):
            if c.category_id:
                category_ids.add(c.category_id)
            campaigns.append({
                "id": c.id,
                "name": c.name,
                "code": c.code,
                "category_id": c.category_id,
                "subcategory_id": c.subcategory_id,
                "budget_id": c.budget_id,
                "approved_amount": str(c.approved_amount or 0),
            })

        categories = [
            {
                "id": c.id,
                "name": c.name,
                "code": c.code,
            }
            for c in BudgetCategory.objects.filter(
                id__in=category_ids,
                org_id=opt.entity.org_id,
                is_active=True,
            ).order_by("name")
        ]

        budgets = [
            {
                "id": b.id,
                "name": str(b),
                "category_id": b.category_id,
                "subcategory_id": b.subcategory_id,
                "scope_node_id": b.scope_node_id,
                "scope_node_name": b.scope_node.name if b.scope_node else None,
                "allocated_amount": str(b.allocated_amount),
                "available_amount": str(b.available_amount),
                "currency": b.currency,
            }
            for b in scoped_budgets.filter(status=BudgetStatus.ACTIVE)
        ]

        # Resolve eligible approvers for this entity
        eligible_approvers = []
        if opt.allowed_approvers.exists():
            eligible_approvers = [
                {"id": u.id, "email": u.email, "first_name": u.first_name, "last_name": u.last_name}
                for u in opt.allowed_approvers.all()
            ]
        elif opt.approver_role:
            users = get_users_with_role_at_node(opt.approver_role, opt.entity)
            eligible_approvers = [
                {"id": u.id, "email": u.email, "first_name": u.first_name, "last_name": u.last_name}
                for u in users
            ]

        allowed_entities.append({
            "split_option_id": opt.id,
            "entity_id": opt.entity_id,
            "entity_name": opt.entity.name,
            "business_unit_id": opt.entity_id,
            "business_unit_name": opt.entity.name,
            "eligible_approvers": eligible_approvers,
            "categories": categories,
            "subcategories": subcategories,
            "campaigns": campaigns,
            "budgets": budgets,
            "default_category_id": opt.category_id,
            "default_category_name": opt.category.name if opt.category else None,
            "default_subcategory_id": opt.subcategory_id,
            "default_subcategory_name": opt.subcategory.name if opt.subcategory else None,
            "default_campaign_id": opt.campaign_id,
            "default_campaign_name": opt.campaign.name if opt.campaign else None,
            "default_budget_id": opt.budget_id,
        })

    # Existing allocations for correction context
    existing_allocations = []
    for alloc in InvoiceAllocation.objects.filter(
        split_step=instance_step
    ).select_related("entity", "category", "subcategory", "campaign", "budget", "selected_approver", "branch"):
        existing_allocations.append({
            "id": alloc.id,
            "entity_id": alloc.entity_id,
            "entity_name": alloc.entity.name,
            "category_id": alloc.category_id,
            "subcategory_id": alloc.subcategory_id,
            "campaign_id": alloc.campaign_id,
            "budget_id": alloc.budget_id,
            "amount": str(alloc.amount),
            "selected_approver_id": alloc.selected_approver_id,
            "status": alloc.status,
            "rejection_reason": alloc.rejection_reason,
            "note": alloc.note,
            "branch_id": alloc.branch_id,
            "revision_number": alloc.revision_number,
        })

    return {
        "invoice": invoice_summary,
        "allowed_entities": allowed_entities,
        "existing_allocations": existing_allocations,
        "step_config": {
            "allocation_total_policy": step.allocation_total_policy,
            "require_category": step.require_category,
            "require_subcategory": step.require_subcategory,
            "require_budget": step.require_budget,
            "require_campaign": step.require_campaign,
            "allow_multiple_lines_per_entity": step.allow_multiple_lines_per_entity,
            "approver_selection_mode": step.approver_selection_mode,
        },
    }


# ---------------------------------------------------------------------------
# submit_runtime_invoice_split
# ---------------------------------------------------------------------------

@transaction.atomic
def submit_runtime_invoice_split(instance_step: WorkflowInstanceStep, actor, allocations_payload: list, note: str = "") -> dict:
    """
    Validate and submit runtime invoice split allocations.

    allocations_payload: list of dicts:
        {entity, category?, subcategory?, campaign?, budget?, amount, selected_approver, note?}

    On success:
        - Creates/updates InvoiceAllocation rows
        - Reserves budget per allocation if budget is set
        - Creates WorkflowInstanceBranch per allocation
        - Sets parent split step to WAITING_BRANCHES
        - Emits SPLIT_ALLOCATIONS_SUBMITTED + BRANCH_ASSIGNED events
    """
    from apps.workflow.models import WorkflowSplitOption
    from apps.invoices.models import Invoice, InvoiceAllocation, InvoiceAllocationRevision, InvoiceAllocationStatus
    from apps.budgets.models import Budget, BudgetStatus, SourceType
    from apps.budgets.services import reserve_budget
    from django.contrib.auth import get_user_model
    User = get_user_model()

    step = instance_step.workflow_step
    instance = instance_step.instance_group.instance
    group = instance_step.instance_group

    # --- Guard checks ---
    if step.step_kind != StepKind.RUNTIME_SPLIT_ALLOCATION:
        raise StepActionError(f"Step {step.id} is not RUNTIME_SPLIT_ALLOCATION.")

    if group.status != GroupStatus.IN_PROGRESS:
        raise StepActionError(f"Step group is not IN_PROGRESS (current: {group.status}).")

    if instance_step.status not in (StepStatus.WAITING, StepStatus.WAITING_BRANCHES):
        raise StepActionError(
            f"Step {instance_step.id} has status {instance_step.status}; expected WAITING or WAITING_BRANCHES."
        )

    if instance_step.assigned_user_id != actor.pk:
        raise StepActionError(f"User {actor} is not the assigned splitter for step {instance_step.id}.")

    if instance.subject_type != "invoice":
        raise StepActionError("Runtime split is only supported for invoice subjects.")

    try:
        invoice = Invoice.objects.get(pk=instance.subject_id)
    except Invoice.DoesNotExist:
        raise StepActionError(f"Invoice {instance.subject_id} not found.")

    if not allocations_payload:
        raise StepActionError("At least one allocation line is required.")

    # --- Load allowed split options ---
    split_options = {
        opt.entity_id: opt
        for opt in WorkflowSplitOption.objects.filter(
            workflow_step=step, is_active=True
        ).prefetch_related("allowed_approvers").select_related("approver_role")
    }

    # --- Validate each allocation line ---
    total = Decimal("0")
    validated_lines = []

    entity_counts: dict[int, int] = {}

    for i, line in enumerate(allocations_payload):
        entity_id = line.get("entity")
        amount_raw = line.get("amount")
        approver_id = line.get("selected_approver")

        if entity_id is None:
            raise StepActionError(f"Line {i}: 'entity' is required.")
        if amount_raw is None:
            raise StepActionError(f"Line {i}: 'amount' is required.")
        if approver_id is None:
            raise StepActionError(f"Line {i}: 'selected_approver' is required.")

        try:
            amount = Decimal(str(amount_raw))
        except Exception:
            raise StepActionError(f"Line {i}: invalid amount '{amount_raw}'.")
        if amount <= 0:
            raise StepActionError(f"Line {i}: amount must be > 0.")

        if entity_id not in split_options:
            raise StepActionError(f"Line {i}: entity {entity_id} is not in configured split options.")

        # Duplicate entity check
        entity_counts[entity_id] = entity_counts.get(entity_id, 0) + 1
        if entity_counts[entity_id] > 1 and not step.allow_multiple_lines_per_entity:
            raise StepActionError(f"Line {i}: duplicate entity {entity_id} not allowed (allow_multiple_lines_per_entity=False).")

        opt = split_options[entity_id]

        # Approver eligibility
        try:
            approver = User.objects.get(pk=approver_id)
        except User.DoesNotExist:
            raise StepActionError(f"Line {i}: approver user {approver_id} not found.")

        if opt.allowed_approvers.exists():
            if not opt.allowed_approvers.filter(pk=approver.pk).exists():
                raise StepActionError(
                    f"Line {i}: user {approver} is not in the allowed approver pool for entity {entity_id}."
                )
        elif opt.approver_role:
            eligible = get_users_with_role_at_node(opt.approver_role, opt.entity)
            if not eligible.filter(pk=approver.pk).exists():
                raise StepActionError(
                    f"Line {i}: user {approver} does not hold role '{opt.approver_role.name}' at entity {entity_id}."
                )

        # Required field checks
        if step.require_category and not line.get("category"):
            raise StepActionError(f"Line {i}: category is required by workflow config.")
        if step.require_subcategory and not line.get("subcategory"):
            raise StepActionError(f"Line {i}: subcategory is required by workflow config.")
        if step.require_budget and not line.get("budget"):
            raise StepActionError(f"Line {i}: budget is required by workflow config.")
        if step.require_campaign and not line.get("campaign"):
            raise StepActionError(f"Line {i}: campaign is required by workflow config.")

        # Budget validity
        budget = None
        if line.get("budget"):
            try:
                budget = Budget.objects.get(pk=line["budget"])
            except Budget.DoesNotExist:
                raise StepActionError(f"Line {i}: budget {line['budget']} not found.")
            if budget.status != BudgetStatus.ACTIVE:
                raise StepActionError(f"Line {i}: budget {budget.id} is not ACTIVE (status={budget.status}).")

        total += amount
        validated_lines.append({**line, "amount": amount, "approver": approver, "budget_obj": budget, "opt": opt})

    # --- Total validation ---
    from apps.workflow.models import AllocationTotalPolicy
    if step.allocation_total_policy == AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL:
        if total != invoice.amount:
            raise StepActionError(
                f"Total allocated amount {total} does not equal invoice amount {invoice.amount}."
            )

    # --- Cancel existing active allocations (correction mode) ---
    now = timezone.now()
    existing_allocs = list(InvoiceAllocation.objects.filter(split_step=instance_step))
    for old in existing_allocs:
        # Release reserved budget if any
        if old.budget_id and old.status in (
            InvoiceAllocationStatus.SUBMITTED,
            InvoiceAllocationStatus.BRANCH_PENDING,
            InvoiceAllocationStatus.CORRECTION_REQUIRED,
        ):
            _release_allocation_budget(old, actor)
        # Snapshot as revision
        _snapshot_allocation(old, actor, "correction")
        old.status = InvoiceAllocationStatus.CANCELLED
        old.save(update_fields=["status"])

    # Cancel existing pending branches (correction)
    existing_branches = list(instance_step.branches.filter(status=BranchStatus.PENDING))
    for branch in existing_branches:
        branch.status = BranchStatus.REJECTED
        branch.rejection_reason = "Allocation corrected — branch superseded."
        branch.acted_at = now
        branch.save(update_fields=["status", "rejection_reason", "acted_at"])

    # --- Create allocations and branches ---
    created_allocations = []
    created_branches = []
    budget_results = []

    for idx, line in enumerate(validated_lines):
        opt = line["opt"]
        approver = line["approver"]
        budget_obj = line["budget_obj"]

        alloc = InvoiceAllocation.objects.create(
            invoice=invoice,
            workflow_instance=instance,
            split_step=instance_step,
            entity=opt.entity,
            category_id=line.get("category"),
            subcategory_id=line.get("subcategory"),
            campaign_id=line.get("campaign"),
            budget=budget_obj,
            amount=line["amount"],
            percentage=(line["amount"] / invoice.amount * 100) if invoice.amount else None,
            selected_approver=approver,
            status=InvoiceAllocationStatus.SUBMITTED,
            selected_by=actor,
            selected_at=now,
            note=line.get("note", ""),
            revision_number=1,
        )

        # Reserve budget
        if budget_obj:
            source_id = f"invoice:{invoice.id}:allocation:{alloc.id}"
            try:
                result = reserve_budget(
                    budget=budget_obj,
                    amount=line["amount"],
                    source_type=SourceType.INVOICE,
                    source_id=source_id,
                    requested_by=actor,
                    note=f"Runtime split allocation for invoice {invoice.id}",
                )
                budget_results.append({
                    "allocation_id": alloc.id,
                    "status": result["status"],
                    "projected_utilization": str(result["projected_utilization"]),
                })
                _emit_event(
                    instance, WorkflowEventType.ALLOCATION_BUDGET_RESERVED, actor,
                    metadata={"allocation_id": alloc.id, "budget_id": budget_obj.id, "amount": str(line["amount"])},
                )
            except Exception as e:
                raise StepActionError(f"Budget reservation failed for line {idx}: {e}")

        # Create branch
        branch = WorkflowInstanceBranch.objects.create(
            parent_instance_step=instance_step,
            instance=instance,
            target_scope_node=opt.entity,
            branch_index=idx,
            status=BranchStatus.PENDING,
            assigned_user=approver,
            assignment_state=AssignmentState.ASSIGNED,
        )

        # Link branch to allocation
        alloc.branch = branch
        alloc.status = InvoiceAllocationStatus.BRANCH_PENDING
        alloc.save(update_fields=["branch", "status"])

        created_allocations.append(alloc)
        created_branches.append(branch)

        _emit_event(
            instance, WorkflowEventType.BRANCH_ASSIGNED, actor,
            target_user=approver,
            metadata={"branch_id": branch.id, "allocation_id": alloc.id},
        )

    # --- Mark split step as WAITING_BRANCHES ---
    instance_step.status = StepStatus.WAITING_BRANCHES
    instance_step.save(update_fields=["status"])

    _emit_event(
        instance, WorkflowEventType.SPLIT_ALLOCATIONS_SUBMITTED, actor,
        metadata={
            "instance_step_id": instance_step.id,
            "allocation_count": len(created_allocations),
            "allocation_ids": [a.id for a in created_allocations],
            "branch_ids": [b.id for b in created_branches],
            "total_amount": str(total),
            "note": note,
        },
    )

    return {
        "allocations": [
            {
                "id": a.id,
                "entity_id": a.entity_id,
                "amount": str(a.amount),
                "status": a.status,
                "branch_id": a.branch_id,
            }
            for a in created_allocations
        ],
        "branches": [
            {"id": b.id, "target_scope_node_id": b.target_scope_node_id, "assigned_user_id": b.assigned_user_id}
            for b in created_branches
        ],
        "budget_reservation_results": budget_results,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _release_allocation_budget(allocation, actor):
    """Release reserved budget for a single allocation being corrected/cancelled."""
    from apps.budgets.models import SourceType
    from apps.budgets.services import release_reserved_budget
    from apps.workflow.models import WorkflowEventType

    if not allocation.budget_id:
        return
    source_id = f"invoice:{allocation.invoice_id}:allocation:{allocation.id}"
    try:
        release_reserved_budget(
            budget=allocation.budget,
            source_type=SourceType.INVOICE,
            source_id=source_id,
            released_by=actor,
            note=f"Allocation {allocation.id} cancelled/corrected",
        )
        _emit_event(
            allocation.workflow_instance,
            WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
            actor,
            metadata={"allocation_id": allocation.id, "budget_id": allocation.budget_id},
        )
    except Exception:
        pass  # Budget release failures are non-fatal; ops can correct manually


def _snapshot_allocation(allocation, actor, reason: str):
    """Save current allocation state as a revision before overwriting."""
    from apps.invoices.models import InvoiceAllocationRevision
    InvoiceAllocationRevision.objects.create(
        allocation=allocation,
        revision_number=allocation.revision_number,
        snapshot={
            "entity_id": allocation.entity_id,
            "category_id": allocation.category_id,
            "subcategory_id": allocation.subcategory_id,
            "campaign_id": allocation.campaign_id,
            "budget_id": allocation.budget_id,
            "amount": str(allocation.amount),
            "selected_approver_id": allocation.selected_approver_id,
            "status": allocation.status,
            "note": allocation.note,
        },
        changed_by=actor,
        change_reason=reason,
    )


# ---------------------------------------------------------------------------
# Branch rejection behavior for RUNTIME_SPLIT_ALLOCATION
# ---------------------------------------------------------------------------

@transaction.atomic
def handle_runtime_split_branch_rejection(branch: WorkflowInstanceBranch, acted_by, note: str = ""):
    """
    When a branch created from a RUNTIME_SPLIT_ALLOCATION step is rejected:
    - Mark that allocation as CORRECTION_REQUIRED (not the whole invoice).
    - Return the parent split step to WAITING state so the splitter can correct.
    - Do NOT reject the whole instance unless the step's rejection_action is TERMINATE.
    """
    from apps.invoices.models import InvoiceAllocation, InvoiceAllocationStatus
    from apps.workflow.models import RejectionAction

    now = timezone.now()
    instance = branch.instance
    parent_step = branch.parent_instance_step
    group = parent_step.instance_group
    step = parent_step.workflow_step

    # Determine rejection behavior from the group's on_rejection_action
    rejection_action = group.step_group.on_rejection_action

    # Update the allocation linked to this branch
    try:
        alloc = branch.invoice_allocation
        alloc.status = InvoiceAllocationStatus.CORRECTION_REQUIRED
        alloc.rejected_by = acted_by
        alloc.rejected_at = now
        alloc.rejection_reason = note
        alloc.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason"])
    except Exception:
        alloc = None

    if rejection_action == RejectionAction.TERMINATE:
        # Hard terminate — same as before
        parent_step.status = StepStatus.REJECTED
        parent_step.acted_at = now
        parent_step.save(update_fields=["status", "acted_at"])
        group.status = GroupStatus.REJECTED
        group.save(update_fields=["status"])
        instance.status = "REJECTED"
        instance.completed_at = now
        instance.save(update_fields=["status", "completed_at"])
        # Release budgets for any allocated-but-not-consumed allocations
        for a in InvoiceAllocation.objects.filter(workflow_instance=instance).select_related("budget"):
            if a.budget_id:
                try:
                    source_id = f"invoice:{a.invoice_id}:allocation:{a.id}"
                    from apps.budgets.services import release_reserved_budget
                    from apps.budgets.models import SourceType
                    release_reserved_budget(
                        budget=a.budget,
                        source_type=SourceType.INVOICE,
                        source_id=source_id,
                        released_by=acted_by,
                        note=f"Instance {instance.id} terminated — budget released",
                    )
                except Exception:
                    pass
        from apps.workflow.services import _emit_event, _sync_subject_status_on_workflow_change
        _emit_event(instance, WorkflowEventType.INSTANCE_REJECTED, acted_by)
        _sync_subject_status_on_workflow_change(instance)
        return

    # BRANCH_CORRECTION / RETURN_TO_SPLITTER — return parent step to WAITING for correction
    # Keep other APPROVED branches intact; only reset this branch's allocation
    all_branches = list(parent_step.branches.exclude(pk=branch.pk))
    any_still_pending = any(b.status == BranchStatus.PENDING for b in all_branches)

    if not any_still_pending:
        # All other branches resolved; put parent step back to WAITING for splitter correction
        parent_step.status = StepStatus.WAITING
        parent_step.save(update_fields=["status"])

    _emit_event(
        instance, WorkflowEventType.BRANCH_REJECTED, acted_by,
        metadata={
            "branch_id": branch.id,
            "allocation_id": alloc.id if alloc else None,
            "note": note,
            "rejection_action": rejection_action,
            "return_to_splitter": True,
        },
    )
