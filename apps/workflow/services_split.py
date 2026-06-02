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
from apps.workflow.services import (
    StepActionError,
    _emit_event,
    _advance_on_group_complete as _advance_on_group_complete_util,
    _release_allocation_budgets,
)


def _enrich_with_live_balances(budgets: list, budget_lines: list) -> None:
    """
    Mutate budgets[] and budget_lines[] in-place: staple ledger-derived
    reserved_amount, consumed_amount, available_amount onto each dict.
    Replaces the denormalized available_amount on budgets with the ledger figure.
    """
    from apps.budgets.selectors import _ledger_balances_for_budgets, _ledger_balances_for_lines

    budget_ids = [b["id"] for b in budgets]
    line_ids = [ln["id"] for ln in budget_lines]

    budget_balances = _ledger_balances_for_budgets(budget_ids)
    line_balances = _ledger_balances_for_lines(line_ids)

    zero = {"reserved": Decimal("0"), "consumed": Decimal("0"), "available": Decimal("0")}
    for b in budgets:
        bal = budget_balances.get(b["id"], zero)
        b["reserved_amount"] = str(bal["reserved"])
        b["consumed_amount"] = str(bal["consumed"])
        b["available_amount"] = str(bal["available"])  # overwrite denormalized

    for ln in budget_lines:
        bal = line_balances.get(ln["id"], zero)
        ln["reserved_amount"] = str(bal["reserved"])
        ln["consumed_amount"] = str(bal["consumed"])
        ln["available_amount"] = str(bal["available"])


def _scope_matches_entity_or_descendant(entity, scope_node) -> bool:
    """
    Accept budgets scoped either:
      - at the entity itself, OR
      - within the entity subtree.

    This enforces BU-owned budget behavior. Parent-scope budgets are no longer
    considered valid funding sources for child BU allocations.

    The check requires exact match OR descendant (with trailing slash) to prevent
    "Farukhnagar I" from matching "Farukhnagar II" via prefix collision.
    """
    if not scope_node:
        return False
    return (
        scope_node.path == entity.path
        or scope_node.path.startswith(entity.path + "/")
    )


def _get_active_branch_children(entity):
    from apps.core.models import ScopeNode

    return list(
        ScopeNode.objects.filter(parent=entity, is_active=True, node_type="branch").order_by("name")
    )


def _build_split_entity_payload(
    *,
    entity,
    org_id: int,
    split_option_id: int | None,
    eligible_approvers: list,
    approval_required: bool,
    approval_mode: str,
    default_category_id: int | None = None,
    default_category_name: str | None = None,
    default_subcategory_id: int | None = None,
    default_subcategory_name: str | None = None,
    default_campaign_id: int | None = None,
    default_campaign_name: str | None = None,
    default_budget_id: int | None = None,
    parent_entity_id: int | None = None,
    parent_entity_name: str | None = None,
) -> dict:
    base = _build_allowed_entities_for_entity(entity, org_id)
    base.update(
        {
            "split_option_id": split_option_id,
            "eligible_approvers": eligible_approvers,
            "approval_required": approval_required,
            "approval_mode": approval_mode,
            "default_category_id": default_category_id,
            "default_category_name": default_category_name,
            "default_subcategory_id": default_subcategory_id,
            "default_subcategory_name": default_subcategory_name,
            "default_campaign_id": default_campaign_id,
            "default_campaign_name": default_campaign_name,
            "default_budget_id": default_budget_id,
            "node_type": entity.node_type,
            "parent_entity_id": parent_entity_id,
            "parent_entity_name": parent_entity_name,
            "child_entities": [],
        }
    )
    return base


# ---------------------------------------------------------------------------
# get_runtime_split_options
# ---------------------------------------------------------------------------

def _build_allowed_entities_for_entity(
    entity,
    org_id: int,
) -> dict:
    """
    Build the allowed-entity payload for a single ScopeNode.
    Used both for WorkflowSplitOption-based entities and freeform invoice-scope entities.
    Returns categories, subcategories, campaigns, budgets, and budget_lines scoped to the entity.
    """
    from apps.budgets.models import Budget, BudgetLine, BudgetCategory, BudgetStatus
    from apps.campaigns.models import Campaign, CampaignStatus

    scoped_lines = (
        BudgetLine.objects.filter(budget__scope_node__org_id=org_id, budget__status=BudgetStatus.ACTIVE)
        .select_related("budget", "budget__scope_node", "category", "subcategory")
        .order_by("category__name", "subcategory__name")
    )
    scoped_lines = [line for line in scoped_lines if _scope_matches_entity_or_descendant(entity, line.budget.scope_node)]

    category_ids: set[int] = set()
    subcategories = []
    seen_subcategory_ids: set[int] = set()

    for line in scoped_lines:
        category_ids.add(line.category_id)
        if (
            line.subcategory_id
            and line.subcategory
            and line.subcategory.is_active
            and line.subcategory_id not in seen_subcategory_ids
        ):
            seen_subcategory_ids.add(line.subcategory_id)
            subcategories.append({
                "id": line.subcategory_id,
                "name": line.subcategory.name,
                "category_id": line.category_id,
                "category_name": line.category.name if line.category else None,
            })

    scoped_campaigns = (
        Campaign.objects.filter(
            budget__scope_node__org_id=org_id,
            status__in=[
                CampaignStatus.INTERNALLY_APPROVED,
                CampaignStatus.FINANCE_PENDING,
                CampaignStatus.FINANCE_APPROVED,
            ],
        )
        .select_related("category", "subcategory", "budget", "budget__scope_node")
        .order_by("name")
    )
    campaigns = []
    for c in [c for c in scoped_campaigns if c.budget and _scope_matches_entity_or_descendant(entity, c.budget.scope_node)]:
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
            id__in=category_ids, org_id=org_id, is_active=True
        ).order_by("name")
    ]

    budget_ids = set()
    budgets = []
    budget_lines = []
    for line in scoped_lines:
        budget_lines.append({
            "id": line.id,
            "budget_id": line.budget_id,
            "category_id": line.category_id,
            "category_name": line.category.name if line.category else None,
            "subcategory_id": line.subcategory_id,
            "subcategory_name": line.subcategory.name if line.subcategory else None,
            "allocated_amount": str(line.allocated_amount),
        })
        if line.budget_id not in budget_ids:
            budget_ids.add(line.budget_id)
            budgets.append({
                "id": line.budget.id,
                "name": line.budget.name,
                "code": line.budget.code,
                "scope_node_id": line.budget.scope_node_id,
                "scope_node_name": line.budget.scope_node.name if line.budget.scope_node else None,
                "allocated_amount": str(line.budget.allocated_amount),
                "available_amount": str(line.budget.available_amount),
                "currency": line.budget.currency,
            })

    _enrich_with_live_balances(budgets, budget_lines)

    return {
        "split_option_id": None,
        "entity_id": entity.id,
        "entity_name": entity.name,
        "business_unit_id": entity.id,
        "business_unit_name": entity.name,
        "eligible_approvers": [],
        "approval_required": False,
        "approval_mode": "AUTO_APPROVE",
        "categories": categories,
        "subcategories": subcategories,
        "campaigns": campaigns,
        "budgets": budgets,
        "budget_lines": budget_lines,
        "default_category_id": None,
        "default_category_name": None,
        "default_subcategory_id": None,
        "default_subcategory_name": None,
        "default_campaign_id": None,
        "default_campaign_name": None,
        "default_budget_id": None,
    }


def get_runtime_split_options(instance_step: WorkflowInstanceStep, user) -> dict:
    """
    Return all the data the frontend needs to render the split allocation form:
      - invoice summary (amount, currency, title, vendor)
      - allowed entities: from WorkflowSplitOption config, OR (when no options exist)
        from the invoice's scope hierarchy (invoice scope node + active direct children).
        The latter enables freeform allocation without pre-configured split options.
      - eligible approvers per entity (empty when no options are configured)
      - existing draft/correction allocations for this step (if any)
    """
    from apps.workflow.models import WorkflowSplitOption
    from apps.invoices.models import Invoice, InvoiceAllocation
    from apps.budgets.models import BudgetStatus
    from apps.core.models import ScopeNode

    step = instance_step.workflow_step
    instance = instance_step.instance_group.instance

    if step.step_kind != StepKind.RUNTIME_SPLIT_ALLOCATION:
        raise StepActionError(
            f"Step {step.id} is not a RUNTIME_SPLIT_ALLOCATION step (kind={step.step_kind})."
        )

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

    split_options_qs = WorkflowSplitOption.objects.filter(
        workflow_step=step, is_active=True
    ).select_related(
        "entity", "approver_role", "category", "subcategory", "campaign", "budget"
    ).prefetch_related("allowed_approvers").order_by("display_order")

    allowed_entities = []
    org_id = invoice.scope_node.org_id

    if split_options_qs.exists():
        for opt in split_options_qs:
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
            else:
                eligible_approvers = []

            approval_required = _is_approval_required_for_option(step, opt)
            approval_mode = _get_approval_mode_label(step.branch_approval_policy, opt)

            region_payload = _build_split_entity_payload(
                entity=opt.entity,
                org_id=opt.entity.org_id,
                split_option_id=opt.id,
                eligible_approvers=eligible_approvers,
                approval_required=approval_required,
                approval_mode=approval_mode,
                default_category_id=opt.category_id,
                default_category_name=opt.category.name if opt.category else None,
                default_subcategory_id=opt.subcategory_id,
                default_subcategory_name=opt.subcategory.name if opt.subcategory else None,
                default_campaign_id=opt.campaign_id,
                default_campaign_name=opt.campaign.name if opt.campaign else None,
                default_budget_id=opt.budget_id,
            )

            child_branches = _get_active_branch_children(opt.entity)
            if child_branches:
                region_payload["child_entities"] = [
                    _build_split_entity_payload(
                        entity=child,
                        org_id=child.org_id,
                        split_option_id=opt.id,
                        eligible_approvers=eligible_approvers,
                        approval_required=approval_required,
                        approval_mode=approval_mode,
                        parent_entity_id=opt.entity_id,
                        parent_entity_name=opt.entity.name,
                    )
                    for child in child_branches
                ]

            allowed_entities.append(region_payload)
    else:
        freeform_payload = _build_split_entity_payload(
            entity=invoice.scope_node,
            org_id=org_id,
            split_option_id=None,
            eligible_approvers=[],
            approval_required=False,
            approval_mode="AUTO_APPROVE",
        )
        child_branches = _get_active_branch_children(invoice.scope_node)
        if child_branches:
            freeform_payload["child_entities"] = [
                _build_split_entity_payload(
                    entity=child,
                    org_id=child.org_id,
                    split_option_id=None,
                    eligible_approvers=[],
                    approval_required=False,
                    approval_mode="AUTO_APPROVE",
                    parent_entity_id=invoice.scope_node_id,
                    parent_entity_name=invoice.scope_node.name,
                )
                for child in child_branches
            ]
        allowed_entities.append(freeform_payload)

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
            "branch_approval_policy": step.branch_approval_policy,
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
    from apps.budgets.services import reserve_budget_line, resolve_budget_line_for_allocation, BudgetLineNotFoundError
    from apps.core.models import ScopeNode
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
    split_options_qs = list(
        WorkflowSplitOption.objects.filter(
            workflow_step=step, is_active=True
        ).prefetch_related("allowed_approvers").select_related("approver_role", "entity")
    )
    split_options = {opt.entity_id: opt for opt in split_options_qs}
    is_freeform = not split_options  # True when no WorkflowSplitOption rows; enables freeform allocation

    allowed_entity_bindings: dict[int, tuple[object | None, object]] = {}
    freeform_root_children = _get_active_branch_children(invoice.scope_node) if is_freeform else []
    if is_freeform:
        if freeform_root_children:
            for child in freeform_root_children:
                allowed_entity_bindings[child.id] = (None, child)
        else:
            allowed_entity_bindings[invoice.scope_node_id] = (None, invoice.scope_node)
    else:
        for opt in split_options_qs:
            child_branches = _get_active_branch_children(opt.entity)
            if child_branches:
                for child in child_branches:
                    allowed_entity_bindings[child.id] = (opt, child)
            else:
                allowed_entity_bindings[opt.entity_id] = (opt, opt.entity)

    # --- Validate each allocation line ---
    total = Decimal("0")
    validated_lines = []

    entity_counts: dict[int, int] = {}
    entity_approver_ids: dict[int, int | None] = {}
    seen_allocation_paths: set[tuple[int, int | None, int | None, int | None]] = set()

    for i, line in enumerate(allocations_payload):
        entity_id = line.get("entity")
        amount_raw = line.get("amount")
        approver_id = line.get("selected_approver")

        if entity_id is None:
            raise StepActionError(f"Line {i}: 'entity' is required.")
        if amount_raw is None:
            raise StepActionError(f"Line {i}: 'amount' is required.")

        try:
            amount = Decimal(str(amount_raw))
        except Exception:
            raise StepActionError(f"Line {i}: invalid amount '{amount_raw}'.")
        if amount <= 0:
            raise StepActionError(f"Line {i}: amount must be > 0.")

        if entity_id not in allowed_entity_bindings:
            raise StepActionError(f"Line {i}: entity {entity_id} is not allowed for this split step.")

        opt, actual_entity = allowed_entity_bindings[entity_id]
        if is_freeform:
            approval_required = False
        else:
            approval_required = _is_approval_required_for_option(step, opt)

        # Duplicate entity check
        entity_counts[entity_id] = entity_counts.get(entity_id, 0) + 1
        if entity_counts[entity_id] > 1 and not step.allow_multiple_lines_per_entity:
            raise StepActionError(f"Line {i}: duplicate entity {entity_id} not allowed (allow_multiple_lines_per_entity=False).")

        approver = None
        if approval_required:
            if approver_id is None:
                raise StepActionError(f"Line {i}: 'selected_approver' is required for entity {entity_id}.")
            try:
                approver = User.objects.get(pk=approver_id)
            except User.DoesNotExist:
                raise StepActionError(f"Line {i}: approver user {approver_id} not found.")

            if opt and opt.allowed_approvers.exists():
                if not opt.allowed_approvers.filter(pk=approver.pk).exists():
                    raise StepActionError(
                        f"Line {i}: user {approver} is not in the allowed approver pool for entity {entity_id}."
                    )
            elif opt and opt.approver_role:
                eligible = get_users_with_role_at_node(opt.approver_role, opt.entity)
                if not eligible.filter(pk=approver.pk).exists():
                    raise StepActionError(
                        f"Line {i}: user {approver} does not hold role '{opt.approver_role.name}' at entity {entity_id}."
                    )
            if entity_id in entity_approver_ids and entity_approver_ids[entity_id] != approver.pk:
                raise StepActionError(
                    f"Line {i}: all allocations for the same entity must use the same approver."
                )
            entity_approver_ids[entity_id] = approver.pk
        else:
            # Approval not required — approver may be omitted; we'll auto-approve
            if approver_id is not None:
                try:
                    approver = User.objects.get(pk=approver_id)
                except User.DoesNotExist:
                    raise StepActionError(f"Line {i}: approver user {approver_id} not found.")
                # Permitted but not used for auto-approve lines

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
            if budget.scope_node and not _scope_matches_entity_or_descendant(actual_entity, budget.scope_node):
                raise StepActionError(
                    f"Line {i}: budget {budget.id} is not within the scope of entity {actual_entity.id}."
                )

        allocation_path = (
            entity_id,
            budget.id if budget else None,
            line.get("category"),
            line.get("subcategory"),
        )
        if allocation_path in seen_allocation_paths:
            raise StepActionError(
                f"Line {i}: duplicate allocation path is not allowed for the same entity/budget/category/subcategory combination."
            )
        seen_allocation_paths.add(allocation_path)

        total += amount
        validated_lines.append({
            **line,
            "amount": amount,
            "approver": approver,
            "budget_obj": budget,
            "opt": opt,
            "actual_entity": actual_entity,
            "approval_required": approval_required,
            "is_freeform": is_freeform,
        })

    # --- Total validation ---
    from apps.workflow.models import AllocationTotalPolicy
    if step.allocation_total_policy == AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL:
        if total != invoice.amount:
            raise StepActionError(
                f"Total allocated amount {total} does not equal invoice amount {invoice.amount}."
            )

    # --- Cancel existing active allocations (correction mode) ---
    now = timezone.now()
    existing_allocs = list(
        InvoiceAllocation.objects.filter(
            split_step=instance_step,
            status__in=(
                InvoiceAllocationStatus.SUBMITTED,
                InvoiceAllocationStatus.BRANCH_PENDING,
                InvoiceAllocationStatus.APPROVED,
                InvoiceAllocationStatus.CORRECTION_REQUIRED,
            ),
        )
    )
    for old in existing_allocs:
        # Release reserved budget if any (includes APPROVED — budget stays RESERVED until invoice final approval)
        if old.budget_id and old.status in (
            InvoiceAllocationStatus.SUBMITTED,
            InvoiceAllocationStatus.BRANCH_PENDING,
            InvoiceAllocationStatus.APPROVED,
            InvoiceAllocationStatus.CORRECTION_REQUIRED,
        ):
            _release_allocation_budget(old, actor)
        # Snapshot as revision
        _snapshot_allocation(old, actor, "correction")
        old.status = InvoiceAllocationStatus.CANCELLED
        old.save(update_fields=["status"])

    # Delete ALL existing branches so same-entity resubmission doesn't violate
    # the (parent_instance_step, target_scope_node) unique constraint
    instance_step.branches.all().delete()

    # --- Create allocations and branches ---
    created_allocations = []
    created_branches = []
    budget_results = []
    any_pending = False
    freeform_branch = None  # shared branch for all freeform rows (one branch per step+scope_node pair)
    pending_branches_by_entity: dict[int, WorkflowInstanceBranch] = {}
    approved_branches_by_entity: dict[int, WorkflowInstanceBranch] = {}

    for idx, line in enumerate(validated_lines):
        opt = line["opt"]
        approver = line["approver"]
        budget_obj = line["budget_obj"]
        approval_required = line["approval_required"]
        line_is_freeform = line.get("is_freeform", False)
        entity = line["actual_entity"]

        if approval_required:
            # Normal path: pending branch assigned to approver
            alloc = InvoiceAllocation.objects.create(
                invoice=invoice,
                workflow_instance=instance,
                split_step=instance_step,
                entity=entity,
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
                    budget_line = resolve_budget_line_for_allocation(
                        budget=budget_obj,
                        category_id=line.get("category"),
                        subcategory_id=line.get("subcategory"),
                    )
                except BudgetLineNotFoundError as e:
                    raise StepActionError(f"Budget reservation failed for line {idx}: {e}")

                try:
                    result = reserve_budget_line(
                        line=budget_line,
                        amount=line["amount"],
                        source_type=SourceType.INVOICE,
                        source_id=source_id,
                        requested_by=actor,
                        note=f"Runtime split allocation for invoice {invoice.id}",
                    )
                except Exception as e:
                    raise StepActionError(f"Budget reservation failed for line {idx}: {e}")

                if result["status"] == "variance_required":
                    raise StepActionError(
                        f"Line {idx}: budget {budget_obj.id} requires variance approval "
                        f"(projected utilization: {result['projected_utilization']:.2f}%). "
                        "Obtain variance approval and resubmit."
                    )

                budget_results.append({
                    "allocation_id": alloc.id,
                    "budget_line_id": budget_line.id,
                    "status": result["status"],
                    "projected_utilization": str(result["projected_utilization"]),
                })
                _emit_event(
                    instance, WorkflowEventType.ALLOCATION_BUDGET_RESERVED, actor,
                    metadata={
                        "allocation_id": alloc.id,
                        "budget_id": budget_obj.id,
                        "budget_line_id": budget_line.id,
                        "amount": str(line["amount"]),
                    },
                )

            branch = pending_branches_by_entity.get(entity.id)
            if branch is None:
                branch = WorkflowInstanceBranch.objects.create(
                    parent_instance_step=instance_step,
                    instance=instance,
                    target_scope_node=entity,
                    branch_index=idx,
                    status=BranchStatus.PENDING,
                    assigned_user=approver,
                    assignment_state=AssignmentState.ASSIGNED,
                )
                pending_branches_by_entity[entity.id] = branch
                created_branches.append(branch)
                alloc.branch = branch
                alloc.status = InvoiceAllocationStatus.BRANCH_PENDING
                alloc.save(update_fields=["branch", "status"])

                _emit_event(
                    instance, WorkflowEventType.BRANCH_ASSIGNED, actor,
                    target_user=approver,
                    metadata={"branch_id": branch.id, "allocation_id": alloc.id},
                )
            else:
                alloc.status = InvoiceAllocationStatus.BRANCH_PENDING
                alloc.save(update_fields=["status"])

            created_allocations.append(alloc)
            any_pending = True
        else:
            # Auto-approve: allocation is immediately approved
            # Budget reservation still happens
            alloc = InvoiceAllocation.objects.create(
                invoice=invoice,
                workflow_instance=instance,
                split_step=instance_step,
                entity=entity,
                category_id=line.get("category"),
                subcategory_id=line.get("subcategory"),
                campaign_id=line.get("campaign"),
                budget=budget_obj,
                amount=line["amount"],
                percentage=(line["amount"] / invoice.amount * 100) if invoice.amount else None,
                selected_approver=None,
                status=InvoiceAllocationStatus.APPROVED,
                selected_by=actor,
                selected_at=now,
                approved_by=actor,
                approved_at=now,
                note=line.get("note", ""),
                revision_number=1,
            )
            # Budget reservation still happens for auto-approved allocations
            if budget_obj:
                source_id = f"invoice:{invoice.id}:allocation:{alloc.id}"
                try:
                    budget_line = resolve_budget_line_for_allocation(
                        budget=budget_obj,
                        category_id=line.get("category"),
                        subcategory_id=line.get("subcategory"),
                    )
                except BudgetLineNotFoundError as e:
                    raise StepActionError(f"Budget reservation failed for line {idx}: {e}")

                try:
                    result = reserve_budget_line(
                        line=budget_line,
                        amount=line["amount"],
                        source_type=SourceType.INVOICE,
                        source_id=source_id,
                        requested_by=actor,
                        note=f"Runtime split allocation for invoice {invoice.id} (auto-approved)",
                    )
                except Exception as e:
                    raise StepActionError(f"Budget reservation failed for line {idx}: {e}")

                if result["status"] == "variance_required":
                    raise StepActionError(
                        f"Line {idx}: budget {budget_obj.id} requires variance approval "
                        f"(projected utilization: {result['projected_utilization']:.2f}%). "
                        "Obtain variance approval and resubmit."
                    )

                budget_results.append({
                    "allocation_id": alloc.id,
                    "budget_line_id": budget_line.id,
                    "status": result["status"],
                    "projected_utilization": str(result["projected_utilization"]),
                })
                _emit_event(
                    instance, WorkflowEventType.ALLOCATION_BUDGET_RESERVED, actor,
                    metadata={
                        "allocation_id": alloc.id,
                        "budget_id": budget_obj.id,
                        "budget_line_id": budget_line.id,
                        "amount": str(line["amount"]),
                    },
                )

            # Create or reuse an APPROVED branch for audit consistency
            if line_is_freeform:
                # Freeform: multiple rows share one branch to satisfy the (step, scope_node)
                # unique constraint. InvoiceAllocation.branch is OneToOneField — only the first
                # row links to the branch; subsequent rows leave branch=None.
                if freeform_branch is None:
                    freeform_branch = WorkflowInstanceBranch.objects.create(
                        parent_instance_step=instance_step,
                        instance=instance,
                        target_scope_node=entity,
                        branch_index=0,
                        status=BranchStatus.APPROVED,
                        assigned_user=None,
                        assignment_state=AssignmentState.ASSIGNED,
                        acted_at=now,
                        note="Auto-approved: freeform allocation, no branch approver configured.",
                    )
                    created_branches.append(freeform_branch)
                    alloc.branch = freeform_branch
                    alloc.save(update_fields=["branch"])
                branch = freeform_branch  # for emit_event below
            else:
                branch = approved_branches_by_entity.get(entity.id)
                if branch is None:
                    branch = WorkflowInstanceBranch.objects.create(
                        parent_instance_step=instance_step,
                        instance=instance,
                        target_scope_node=entity,
                        branch_index=idx,
                        status=BranchStatus.APPROVED,
                        assigned_user=None,
                        assignment_state=AssignmentState.ASSIGNED,
                        acted_at=now,
                        note="Auto-approved: no branch approver configured.",
                    )
                    approved_branches_by_entity[entity.id] = branch
                    alloc.branch = branch
                    alloc.save(update_fields=["branch"])
                    created_branches.append(branch)

            created_allocations.append(alloc)
            # any_pending stays False

            _emit_event(
                instance, WorkflowEventType.BRANCH_ASSIGNED, actor,
                metadata={
                    "branch_id": branch.id,
                    "allocation_id": alloc.id,
                    "auto_approved": True,
                },
            )

    # --- Update parent split step status ---
    if any_pending:
        instance_step.status = StepStatus.WAITING_BRANCHES
        instance_step.save(update_fields=["status"])
    else:
        # All allocations auto-approved — step is immediately complete
        instance_step.status = StepStatus.APPROVED
        instance_step.acted_at = now
        instance_step.note = "All split allocations auto-approved."
        instance_step.save(update_fields=["status", "acted_at", "note"])
        # Emit BRANCHES_JOINED so workflow advancement is traceable
        _emit_event(
            instance, WorkflowEventType.BRANCHES_JOINED, actor,
            metadata={
                "instance_step_id": instance_step.id,
                "branch_ids": [b.id for b in created_branches],
                "allocation_ids": [a.id for a in created_allocations],
                "all_auto_approved": True,
            },
        )
        _advance_on_group_complete_util(instance_step.instance_group, instance, actor)
        _emit_event(
            instance, WorkflowEventType.SPLIT_ALLOCATIONS_SUBMITTED, actor,
            metadata={
                "instance_step_id": instance_step.id,
                "allocation_count": len(created_allocations),
                "allocation_ids": [a.id for a in created_allocations],
                "branch_ids": [b.id for b in created_branches],
                "total_amount": str(total),
                "note": note,
                "all_auto_approved": True,
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
                    "auto_approved": True,
                }
                for a in created_allocations
            ],
            "branches": [
                {"id": b.id, "target_scope_node_id": b.target_scope_node_id, "assigned_user_id": b.assigned_user_id, "status": b.status}
                for b in created_branches
            ],
            "budget_reservation_results": budget_results,
        }

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
            {"id": b.id, "target_scope_node_id": b.target_scope_node_id, "assigned_user_id": b.assigned_user_id, "status": b.status}
            for b in created_branches
        ],
        "budget_reservation_results": budget_results,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_approval_required_for_option(step, opt) -> bool:
    """
    Returns True when a branch approver is required for the given split option,
    based on the step's branch_approval_policy.
    """
    from apps.workflow.models import BranchApprovalPolicy
    policy = step.branch_approval_policy

    if policy == BranchApprovalPolicy.REQUIRED_FOR_ALL:
        return True

    if policy == BranchApprovalPolicy.OPTIONAL_WHEN_CONFIGURED:
        # Approval required only when the option has an approver configured
        return bool(opt.approver_role or opt.allowed_approvers.exists())

    # SKIP_ALL — no approval required ever
    return False


def _get_approval_mode_label(policy, opt) -> str:
    """
    Returns a human-readable label describing the approval mode for this option.
    """
    from apps.workflow.models import BranchApprovalPolicy
    if policy == BranchApprovalPolicy.REQUIRED_FOR_ALL:
        return "REQUIRED_FOR_ALL"
    if policy == BranchApprovalPolicy.SKIP_ALL:
        return "SKIP_ALL"
    # OPTIONAL_WHEN_CONFIGURED
    if opt.approver_role or opt.allowed_approvers.exists():
        return "REQUIRED_FOR_ALL"  # actually required in this config
    return "AUTO_APPROVE_NO_APPROVER_CONFIG"


def _release_allocation_budget(allocation, actor):
    """Release reserved budget for a single allocation being corrected/cancelled."""
    from apps.budgets.models import SourceType
    from apps.budgets.services import (
        release_reserved_budget,
        release_reserved_budget_line,
        get_source_reserved_balance,
        get_source_reserved_balance_for_line,
        resolve_budget_line_for_allocation,
        BudgetLineNotFoundError,
    )
    from apps.workflow.models import WorkflowEventType

    if not allocation.budget_id:
        return
    source_id = f"invoice:{allocation.invoice_id}:allocation:{allocation.id}"

    # Try line-level release first (new allocations), fall back to header (legacy)
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
                note=f"Allocation {allocation.id} cancelled/corrected",
            )
            _emit_event(
                allocation.workflow_instance,
                WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
                actor,
                metadata={
                    "allocation_id": allocation.id,
                    "budget_id": allocation.budget_id,
                    "budget_line_id": budget_line.id,
                },
            )
    except BudgetLineNotFoundError:
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
                    note=f"Allocation {allocation.id} cancelled/corrected (legacy)",
                )
                _emit_event(
                    allocation.workflow_instance,
                    WorkflowEventType.ALLOCATION_BUDGET_RELEASED,
                    actor,
                    metadata={"allocation_id": allocation.id, "budget_id": allocation.budget_id},
                )
        except Exception:
            pass  # Budget release failures are non-fatal; ops can correct manually
    except Exception:
        pass  # Budget release failures are non-fatal; ops can correct manually


def _snapshot_allocation(allocation, actor, reason: str):
    """Save current allocation state as a revision before overwriting."""
    from apps.invoices.models import InvoiceAllocationRevision
    latest_revision = allocation.revisions.order_by("-revision_number").values_list("revision_number", flat=True).first()
    next_revision_number = (latest_revision or 0) + 1
    InvoiceAllocationRevision.objects.create(
        allocation=allocation,
        revision_number=next_revision_number,
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

    # Update all allocations represented by this branch's entity on the split step.
    alloc_qs = InvoiceAllocation.objects.filter(
        split_step=parent_step,
        entity=branch.target_scope_node,
        status__in=(
            InvoiceAllocationStatus.SUBMITTED,
            InvoiceAllocationStatus.BRANCH_PENDING,
            InvoiceAllocationStatus.APPROVED,
        ),
    )
    alloc_ids = list(alloc_qs.values_list("id", flat=True))
    alloc_qs.update(
        status=InvoiceAllocationStatus.CORRECTION_REQUIRED,
        rejected_by=acted_by,
        rejected_at=now,
        rejection_reason=note,
    )
    try:
        alloc = branch.invoice_allocation
    except Exception:
        alloc = None

    if rejection_action == RejectionAction.TERMINATE:
        # Hard terminate
        parent_step.status = StepStatus.REJECTED
        parent_step.acted_at = now
        parent_step.save(update_fields=["status", "acted_at"])
        group.status = GroupStatus.REJECTED
        group.save(update_fields=["status"])
        instance.status = "REJECTED"
        instance.completed_at = now
        instance.save(update_fields=["status", "completed_at"])
        _release_allocation_budgets(instance, acted_by)
        from apps.workflow.services import _sync_subject_status_on_workflow_change
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
            "allocation_ids": alloc_ids,
            "note": note,
            "rejection_action": rejection_action,
            "return_to_splitter": True,
        },
    )
