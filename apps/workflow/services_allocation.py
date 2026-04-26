"""
Single-allocation service.

Handles SINGLE_ALLOCATION step kind:
  - get_single_allocation_options: returns entity options per-entity-scoped (categories,
    subcategories, campaigns, budgets derived from each entity's scope subtree), plus
    step config requirements and any existing allocation for this step.
  - submit_single_invoice_allocation: validates step-config requirements, enforces field
    coherence (entity scope, category/org, subcategory/category, campaign/entity scope,
    budget/entity scope), creates exactly one InvoiceAllocation row covering the full
    invoice amount, reserves budget, advances the step.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.workflow.models import (
    GroupStatus,
    StepKind,
    StepStatus,
    WorkflowEventType,
    WorkflowInstanceStep,
)
from apps.workflow.services import (
    StepActionError,
    _emit_event,
    _advance_on_group_complete as _advance_util,
)


# ---------------------------------------------------------------------------
# get_single_allocation_options
# ---------------------------------------------------------------------------

def get_single_allocation_options(instance_step: WorkflowInstanceStep, user) -> dict:
    """
    Return all the data the frontend needs to render the single-allocation form.

    Response shape mirrors split options so the frontend can use a shared
    allocation UI component:
      - invoice summary (amount, currency, title, vendor)
      - allowed_entities: list of scope nodes (invoice.scope_node + active direct
        children intersected with user's visible scope), each carrying its own
        scoped categories/subcategories/campaigns/budgets
      - step_config: allocation_mode + all require_* flags
      - existing_allocation: previously submitted allocation for this step (if any)
    """
    from apps.invoices.models import Invoice, InvoiceAllocation, InvoiceAllocationStatus
    from apps.budgets.models import Budget, BudgetLine, BudgetCategory, BudgetStatus
    from apps.campaigns.models import Campaign, CampaignStatus
    from apps.core.models import ScopeNode
    from apps.access.selectors import get_user_visible_scope_ids

    step = instance_step.workflow_step
    instance = instance_step.instance_group.instance

    if step.step_kind != StepKind.SINGLE_ALLOCATION:
        raise StepActionError(
            f"Step {step.id} is not a SINGLE_ALLOCATION step (kind={step.step_kind})."
        )
    if instance.subject_type != "invoice":
        raise StepActionError("Single allocation is only supported for invoice subjects.")

    try:
        invoice = Invoice.objects.select_related("scope_node", "scope_node__org", "vendor").get(
            pk=instance.subject_id
        )
    except Invoice.DoesNotExist:
        raise StepActionError(f"Invoice {instance.subject_id} not found.")

    org = invoice.scope_node.org
    visible_ids = set(get_user_visible_scope_ids(user))

    # Allowed entities: invoice's own scope_node + active direct children, filtered to visible
    candidate_ids = {invoice.scope_node_id}
    for child in ScopeNode.objects.filter(parent=invoice.scope_node, is_active=True):
        candidate_ids.add(child.id)
    entity_ids = candidate_ids & visible_ids

    # Build per-entity scoped options (mirrors split allowed_entities shape)
    allowed_entities = []
    active_campaign_statuses = [
        CampaignStatus.INTERNALLY_APPROVED,
        CampaignStatus.FINANCE_PENDING,
        CampaignStatus.FINANCE_APPROVED,
    ]

    for node in ScopeNode.objects.filter(id__in=entity_ids).order_by("name"):
        entity_path = node.path
        target_scope_filter = {
            "budget__scope_node__org_id": org.id,
            "budget__scope_node__path__startswith": entity_path,
        }

        # Active budget lines in this entity's scope subtree
        scoped_lines_qs = (
            BudgetLine.objects.filter(**target_scope_filter, budget__status=BudgetStatus.ACTIVE)
            .select_related("budget", "budget__scope_node", "category", "subcategory")
            .order_by("category__name", "subcategory__name")
        )

        # Collect categories and subcategories from budget lines
        category_ids: set[int] = set()
        subcategories_data: list[dict] = []
        seen_sub_ids: set[int] = set()

        for line in scoped_lines_qs:
            category_ids.add(line.category_id)
            if line.subcategory_id and line.subcategory and line.subcategory.is_active:
                if line.subcategory_id not in seen_sub_ids:
                    seen_sub_ids.add(line.subcategory_id)
                    subcategories_data.append({
                        "id": line.subcategory_id,
                        "name": line.subcategory.name,
                        "code": line.subcategory.code,
                        "category_id": line.category_id,
                    })

        # Active campaigns in this entity's scope subtree; also contribute categories
        scoped_campaigns_qs = (
            Campaign.objects.filter(
                **target_scope_filter,
                status__in=active_campaign_statuses,
            )
            .select_related("category", "subcategory", "budget")
            .order_by("name")
        )
        for c in scoped_campaigns_qs:
            if c.category_id:
                category_ids.add(c.category_id)

        categories_data = [
            {"id": c.id, "name": c.name, "code": c.code}
            for c in BudgetCategory.objects.filter(
                id__in=category_ids, org_id=org.id, is_active=True
            ).order_by("name")
        ]

        # Budget headers for this entity (deduplicated)
        budget_ids = set()
        budgets_data = []
        for line in scoped_lines_qs:
            if line.budget_id not in budget_ids:
                budget_ids.add(line.budget_id)
                budgets_data.append({
                    "id": line.budget.id,
                    "name": line.budget.name,
                    "code": line.budget.code,
                    "scope_node_id": line.budget.scope_node_id,
                    "scope_node_name": line.budget.scope_node.name if line.budget.scope_node else None,
                    "allocated_amount": str(line.budget.allocated_amount),
                    "available_amount": str(line.budget.available_amount),
                    "currency": line.budget.currency,
                })

        # Budget lines grouped by budget for category/subcategory context
        budget_lines_data = [
            {
                "id": line.id,
                "budget_id": line.budget_id,
                "category_id": line.category_id,
                "category_name": line.category.name if line.category else None,
                "subcategory_id": line.subcategory_id,
                "subcategory_name": line.subcategory.name if line.subcategory else None,
                "allocated_amount": str(line.allocated_amount),
            }
            for line in scoped_lines_qs
        ]

        campaigns_data = [
            {
                "id": c.id,
                "name": c.name,
                "code": c.code,
                "category_id": c.category_id,
                "subcategory_id": c.subcategory_id,
                "budget_id": c.budget_id,
                "approved_amount": str(c.approved_amount or 0),
            }
            for c in scoped_campaigns_qs
        ]

        allowed_entities.append({
            "entity_id": node.id,
            "entity_name": node.name,
            "node_type": node.node_type,
            "categories": categories_data,
            "subcategories": subcategories_data,
            "campaigns": campaigns_data,
            "budgets": budgets_data,
            "budget_lines": budget_lines_data,
        })

    # Existing allocation for this step (correction context)
    existing = None
    active_alloc = (
        InvoiceAllocation.objects.filter(
            split_step=instance_step,
            status__in=(
                InvoiceAllocationStatus.SUBMITTED,
                InvoiceAllocationStatus.APPROVED,
            ),
        )
        .select_related("entity", "category", "subcategory", "campaign", "budget")
        .first()
    )
    if active_alloc:
        existing = {
            "id": active_alloc.id,
            "entity_id": active_alloc.entity_id,
            "entity_name": active_alloc.entity.name if active_alloc.entity else None,
            "category_id": active_alloc.category_id,
            "subcategory_id": active_alloc.subcategory_id,
            "campaign_id": active_alloc.campaign_id,
            "budget_id": active_alloc.budget_id,
            "amount": str(active_alloc.amount),
            "status": active_alloc.status,
            "note": active_alloc.note,
            "revision_number": active_alloc.revision_number,
        }

    return {
        "invoice": {
            "id": invoice.id,
            "title": invoice.title,
            "amount": str(invoice.amount),
            "currency": invoice.currency,
            "vendor_name": invoice.vendor.vendor_name if invoice.vendor else None,
            "scope_node_id": invoice.scope_node_id,
            "scope_node_name": invoice.scope_node.name if invoice.scope_node else None,
        },
        "allowed_entities": allowed_entities,
        "existing_allocation": existing,
        "step_config": {
            "allocation_mode": "SINGLE",
            "amount_locked": True,
            "amount": str(invoice.amount),
            "require_category": step.require_category,
            "require_subcategory": step.require_subcategory,
            "require_budget": step.require_budget,
            "require_campaign": step.require_campaign,
        },
    }


# ---------------------------------------------------------------------------
# submit_single_invoice_allocation
# ---------------------------------------------------------------------------

@transaction.atomic
def submit_single_invoice_allocation(
    instance_step: WorkflowInstanceStep,
    actor,
    payload: dict,
    note: str = "",
) -> dict:
    """
    Validate and submit a single-line invoice allocation.

    payload keys:
        entity         (int, required)  — ScopeNode PK; must be within invoice scope
        category       (int, optional)  — BudgetCategory PK; must belong to invoice org
        subcategory    (int, optional)  — BudgetSubCategory PK; must belong to category
        campaign       (int, optional)  — Campaign PK; must be active and within entity scope
        budget         (int, optional)  — Budget PK; must be ACTIVE and within entity scope
        note           (str, optional)

    Amount is always invoice.amount — not caller-specified.

    Validation layers (in order):
        1. Guard checks (step kind, group/step status, actor)
        2. Entity validation (exists, active, within invoice scope)
        3. Step-config requirements (require_category/subcategory/budget/campaign)
        4. Field coherence (category→org, subcategory→category,
                            campaign→entity scope + active + category match,
                            budget→entity scope + active + category match)

    On success:
        - Cancels any previous active allocation for this step (correction mode)
        - Creates exactly 1 InvoiceAllocation (branch=None, status=APPROVED)
        - Reserves budget if budget is provided
        - Marks step APPROVED
        - Advances the group (may trigger coverage check and instance APPROVED)
    """
    from apps.invoices.models import Invoice, InvoiceAllocation, InvoiceAllocationRevision, InvoiceAllocationStatus
    from apps.budgets.models import BudgetStatus, SourceType, BudgetLine
    from apps.budgets.services import reserve_budget_line, resolve_budget_line_for_allocation, BudgetLineNotFoundError
    from apps.core.models import ScopeNode

    step = instance_step.workflow_step
    instance = instance_step.instance_group.instance
    group = instance_step.instance_group

    # --- Guard checks ---
    if step.step_kind != StepKind.SINGLE_ALLOCATION:
        raise StepActionError(f"Step {step.id} is not a SINGLE_ALLOCATION step.")
    if group.status != GroupStatus.IN_PROGRESS:
        raise StepActionError(f"Step group is not IN_PROGRESS (current: {group.status}).")
    if instance_step.status != StepStatus.WAITING:
        raise StepActionError(
            f"Step {instance_step.id} has status '{instance_step.status}'; expected WAITING."
        )
    if instance_step.assigned_user_id != actor.pk:
        raise StepActionError(
            f"User {actor} is not the assigned allocator for step {instance_step.id}."
        )
    if instance.subject_type != "invoice":
        raise StepActionError("Single allocation is only supported for invoice subjects.")

    try:
        invoice = Invoice.objects.select_related("scope_node", "scope_node__org").get(
            pk=instance.subject_id
        )
    except Invoice.DoesNotExist:
        raise StepActionError(f"Invoice {instance.subject_id} not found.")

    # --- Entity validation ---
    entity_id = payload.get("entity")
    if not entity_id:
        raise StepActionError("'entity' is required.")

    try:
        entity = ScopeNode.objects.get(pk=entity_id, is_active=True)
    except ScopeNode.DoesNotExist:
        raise StepActionError(f"Entity {entity_id} not found or inactive.")

    # Entity must be invoice.scope_node itself or a direct active child
    valid_entity_ids = {invoice.scope_node_id}
    for child in ScopeNode.objects.filter(parent=invoice.scope_node, is_active=True):
        valid_entity_ids.add(child.id)
    if entity.id not in valid_entity_ids:
        raise StepActionError(
            f"Entity {entity.id} is not within the invoice scope "
            f"(must be the invoice node or a direct active child)."
        )

    # --- Step-config requirements ---
    if step.require_category and not payload.get("category"):
        raise StepActionError("category is required by this workflow step's configuration.")
    if step.require_subcategory and not payload.get("subcategory"):
        raise StepActionError("subcategory is required by this workflow step's configuration.")
    if step.require_budget and not payload.get("budget"):
        raise StepActionError("budget is required by this workflow step's configuration.")
    if step.require_campaign and not payload.get("campaign"):
        raise StepActionError("campaign is required by this workflow step's configuration.")

    # --- Category coherence ---
    category_obj = None
    if payload.get("category"):
        from apps.budgets.models import BudgetCategory
        try:
            category_obj = BudgetCategory.objects.get(pk=payload["category"])
        except BudgetCategory.DoesNotExist:
            raise StepActionError(f"Category {payload['category']} not found.")
        if not category_obj.is_active:
            raise StepActionError(f"Category {category_obj.id} is inactive.")
        if category_obj.org_id != invoice.scope_node.org_id:
            raise StepActionError(
                f"Category {category_obj.id} does not belong to this organisation."
            )

    # --- Subcategory coherence ---
    if payload.get("subcategory"):
        from apps.budgets.models import BudgetSubCategory
        try:
            subcategory_obj = BudgetSubCategory.objects.get(pk=payload["subcategory"])
        except BudgetSubCategory.DoesNotExist:
            raise StepActionError(f"Subcategory {payload['subcategory']} not found.")
        if not subcategory_obj.is_active:
            raise StepActionError(f"Subcategory {subcategory_obj.id} is inactive.")
        if category_obj and subcategory_obj.category_id != category_obj.id:
            raise StepActionError(
                f"Subcategory {subcategory_obj.id} does not belong to category {category_obj.id}."
            )

    # --- Campaign coherence ---
    campaign_obj = None
    if payload.get("campaign"):
        from apps.campaigns.models import Campaign, CampaignStatus
        try:
            campaign_obj = Campaign.objects.select_related("scope_node").get(pk=payload["campaign"])
        except Campaign.DoesNotExist:
            raise StepActionError(f"Campaign {payload['campaign']} not found.")
        active_campaign_statuses = [
            CampaignStatus.INTERNALLY_APPROVED,
            CampaignStatus.FINANCE_PENDING,
            CampaignStatus.FINANCE_APPROVED,
        ]
        if campaign_obj.status not in active_campaign_statuses:
            raise StepActionError(
                f"Campaign {campaign_obj.id} is not active (status={campaign_obj.status})."
            )
        if campaign_obj.scope_node and not campaign_obj.scope_node.path.startswith(entity.path):
            raise StepActionError(
                f"Campaign {campaign_obj.id} is not within the scope of entity {entity.id}."
            )
        if category_obj and campaign_obj.category_id and campaign_obj.category_id != category_obj.id:
            raise StepActionError(
                f"Campaign {campaign_obj.id} belongs to category {campaign_obj.category_id}, "
                f"not the selected category {category_obj.id}."
            )

    # --- Budget coherence ---
    budget = None
    if payload.get("budget"):
        from apps.budgets.models import Budget
        try:
            budget = Budget.objects.select_related("scope_node").get(pk=payload["budget"])
        except Budget.DoesNotExist:
            raise StepActionError(f"Budget {payload['budget']} not found.")
        if budget.status != BudgetStatus.ACTIVE:
            raise StepActionError(
                f"Budget {budget.id} is not ACTIVE (status={budget.status})."
            )
        if budget.scope_node and not budget.scope_node.path.startswith(entity.path):
            raise StepActionError(
                f"Budget {budget.id} is not within the scope of entity {entity.id}."
            )
        if category_obj and budget.category_id and budget.category_id != category_obj.id:
            raise StepActionError(
                f"Budget {budget.id} belongs to category {budget.category_id}, "
                f"not the selected category {category_obj.id}."
            )

    now = timezone.now()
    amount = invoice.amount

    # --- Cancel existing active allocation (correction mode) ---
    existing_allocs = list(
        InvoiceAllocation.objects.filter(
            split_step=instance_step,
            status__in=(
                InvoiceAllocationStatus.SUBMITTED,
                InvoiceAllocationStatus.APPROVED,
            ),
        )
    )
    for old in existing_allocs:
        if old.budget_id:
            _release_allocation_budget(old, actor, instance)
        _snapshot_allocation(old, actor, "single_allocation_correction")
        old.status = InvoiceAllocationStatus.CANCELLED
        old.save(update_fields=["status"])

    # --- Create the single allocation ---
    alloc = InvoiceAllocation.objects.create(
        invoice=invoice,
        workflow_instance=instance,
        split_step=instance_step,
        branch=None,
        entity=entity,
        category_id=payload.get("category"),
        subcategory_id=payload.get("subcategory"),
        campaign_id=payload.get("campaign"),
        budget=budget,
        amount=amount,
        percentage=Decimal("100.000"),
        selected_approver=None,
        status=InvoiceAllocationStatus.APPROVED,
        selected_by=actor,
        selected_at=now,
        approved_by=actor,
        approved_at=now,
        note=note or payload.get("note", ""),
        revision_number=len(existing_allocs) + 1,
    )

    # --- Budget reservation ---
    budget_result = None
    if budget:
        source_id = f"invoice:{invoice.id}:allocation:{alloc.id}"
        try:
            # Resolve the BudgetLine from category/subcategory context
            budget_line = resolve_budget_line_for_allocation(
                budget=budget,
                category_id=alloc.category_id,
                subcategory_id=alloc.subcategory_id,
            )
        except BudgetLineNotFoundError as e:
            raise StepActionError(str(e))

        try:
            result = reserve_budget_line(
                line=budget_line,
                amount=amount,
                source_type=SourceType.INVOICE,
                source_id=source_id,
                requested_by=actor,
                note=f"Single allocation for invoice {invoice.id}",
            )
        except Exception as e:
            raise StepActionError(f"Budget reservation failed: {e}")

        if result["status"] == "variance_required":
            raise StepActionError(
                f"Budget {budget.id} requires variance approval before this allocation can "
                f"proceed (projected utilization: {result['projected_utilization']:.2f}%). "
                "Obtain variance approval and resubmit."
            )

        budget_result = {
            "allocation_id": alloc.id,
            "budget_line_id": budget_line.id,
            "status": result["status"],
            "projected_utilization": str(result["projected_utilization"]),
        }
        _emit_event(
            instance,
            WorkflowEventType.ALLOCATION_BUDGET_RESERVED,
            actor,
            metadata={
                "allocation_id": alloc.id,
                "budget_id": budget.id,
                "budget_line_id": budget_line.id,
                "amount": str(amount),
            },
        )

    # --- Emit submission event ---
    _emit_event(
        instance,
        WorkflowEventType.SINGLE_ALLOCATION_SUBMITTED,
        actor,
        metadata={
            "instance_step_id": instance_step.id,
            "allocation_id": alloc.id,
            "amount": str(amount),
            "entity_id": entity.id,
            "note": note,
        },
    )

    # --- Advance step and group ---
    instance_step.status = StepStatus.APPROVED
    instance_step.acted_at = now
    instance_step.note = note or f"Single allocation submitted for {entity.name}"
    instance_step.save(update_fields=["status", "acted_at", "note"])

    _advance_util(group, instance, actor)

    return {
        "allocation": {
            "id": alloc.id,
            "entity_id": alloc.entity_id,
            "entity_name": entity.name,
            "amount": str(alloc.amount),
            "percentage": str(alloc.percentage),
            "category_id": alloc.category_id,
            "subcategory_id": alloc.subcategory_id,
            "campaign_id": alloc.campaign_id,
            "budget_id": alloc.budget_id,
            "status": alloc.status,
            "revision_number": alloc.revision_number,
        },
        "budget_reservation": budget_result,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _release_allocation_budget(allocation, actor, instance):
    """Release reserved budget for a single allocation being corrected."""
    from apps.budgets.models import SourceType
    from apps.budgets.services import (
        release_reserved_budget,
        release_reserved_budget_line,
        get_source_reserved_balance,
        get_source_reserved_balance_for_line,
        resolve_budget_line_for_allocation,
        BudgetLineNotFoundError,
    )

    if not allocation.budget_id:
        return
    source_id = f"invoice:{allocation.invoice_id}:allocation:{allocation.id}"

    # Try line-level release first (new allocations), fall back to header (old allocations)
    try:
        budget_line = resolve_budget_line_for_allocation(
            budget=allocation.budget,
            category_id=allocation.category_id,
            subcategory_id=allocation.subcategory_id,
        )
        balance = get_source_reserved_balance_for_line(
            budget_line, SourceType.INVOICE, source_id
        )
        if balance > 0:
            release_reserved_budget_line(
                line=budget_line,
                amount=balance,
                source_type=SourceType.INVOICE,
                source_id=source_id,
                released_by=actor,
                note=f"Single allocation {allocation.id} corrected/replaced",
            )
            _emit_event(
                instance,
                WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
                actor,
                metadata={
                    "allocation_id": allocation.id,
                    "budget_id": allocation.budget_id,
                    "budget_line_id": budget_line.id,
                },
            )
    except BudgetLineNotFoundError:
        # Fall back to header-level release (legacy allocations)
        try:
            balance = get_source_reserved_balance(
                allocation.budget, SourceType.INVOICE, source_id
            )
            if balance > 0:
                release_reserved_budget(
                    budget=allocation.budget,
                    amount=balance,
                    source_type=SourceType.INVOICE,
                    source_id=source_id,
                    released_by=actor,
                    note=f"Single allocation {allocation.id} corrected/replaced (legacy)",
                )
                _emit_event(
                    instance,
                    WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
                    actor,
                    metadata={
                        "allocation_id": allocation.id,
                        "budget_id": allocation.budget_id,
                    },
                )
        except Exception:
            pass  # Non-fatal; ops can reconcile manually
    except Exception:
        pass  # Non-fatal; ops can reconcile manually


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
            "status": allocation.status,
            "note": allocation.note,
        },
        changed_by=actor,
        change_reason=reason,
    )
