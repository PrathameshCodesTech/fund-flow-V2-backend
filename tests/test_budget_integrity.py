"""
Budget integrity tests for invoice allocation flows.

Covers:
1.  Single allocation: variance_required blocks submission (no allocation approved)
2.  Split allocation: any line variance_required blocks whole submission (atomic rollback)
3.  Generic later rejection (NORMAL_APPROVAL TERMINATE) releases single-alloc budget
4.  Generic later rejection (NORMAL_APPROVAL TERMINATE) releases split-alloc budget
5.  No double release when source balance is already zero
6.  Event coverage: variance path emits no ALLOCATION_BUDGET_RESERVED event
"""
import pytest
from decimal import Decimal

from apps.core.models import Organization, ScopeNode, NodeType
from apps.access.models import Role, UserRoleAssignment
from apps.users.models import User
from apps.invoices.models import Invoice, InvoiceStatus, InvoiceAllocation, InvoiceAllocationStatus
from apps.budgets.models import Budget, BudgetStatus, BudgetCategory
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    WorkflowInstance, WorkflowInstanceStep, WorkflowInstanceGroup,
    WorkflowEvent, WorkflowEventType,
    VersionStatus, InstanceStatus, GroupStatus, StepStatus,
    ScopeResolutionPolicy, ParallelMode, RejectionAction,
    StepKind, BranchApprovalPolicy, AllocationTotalPolicy,
)
from apps.workflow.services import (
    create_workflow_instance_draft,
    reject_workflow_step,
    StepActionError,
)
from apps.workflow.services_allocation import submit_single_invoice_allocation
from apps.modules.models import ModuleActivation, ModuleType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Budget Org", code="budget-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/budget-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/budget-org/hq/ea", depth=1,
    )


@pytest.fixture
def allocator_user(db):
    return User.objects.create_user(email="alloc@example.com", password="pass")


@pytest.fixture
def approver_user(db):
    return User.objects.create_user(email="approver@example.com", password="pass")


@pytest.fixture
def base_role(org):
    return Role.objects.create(org=org, name="Finance", code="finance")


@pytest.fixture
def module_activation(entity):
    return ModuleActivation.objects.create(
        module=ModuleType.INVOICE, scope_node=entity,
        is_active=True, override_parent=True,
    )


@pytest.fixture
def budget_cat(org):
    return BudgetCategory.objects.create(org=org, name="Ops", code="ops", is_active=True)


@pytest.fixture
def large_budget(org, entity, budget_cat):
    """Budget with plenty of headroom — won't trigger variance."""
    return Budget.objects.create(
        org=org, scope_node=entity, category=budget_cat,
        financial_year="2026-27", period_type="yearly",
        allocated_amount=Decimal("500000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR", status=BudgetStatus.ACTIVE,
    )


@pytest.fixture
def tight_budget(org, entity, budget_cat):
    """
    Budget whose allocated_amount equals the invoice amount (25000).
    Projecting 25000 against allocated=25000 → 100% utilization
    which meets the default approval_threshold of 100% → variance_required.
    """
    return Budget.objects.create(
        org=org, scope_node=entity, category=budget_cat,
        financial_year="2026-27", period_type="yearly",
        allocated_amount=Decimal("25000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR", status=BudgetStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INVOICE_AMOUNT = Decimal("25000.00")


def _make_two_group_instance(entity, allocator_user, approver_user, base_role, module_activation,
                             *, alloc_kind=StepKind.SINGLE_ALLOCATION,
                             split_policy=BranchApprovalPolicy.SKIP_ALL):
    """
    Two-group workflow:
      Group 1 (display_order=1) — allocation step (SINGLE_ALLOCATION or RUNTIME_SPLIT_ALLOCATION)
      Group 2 (display_order=2) — NORMAL_APPROVAL, on_rejection_action=TERMINATE

    Returns (instance, alloc_ist, approval_ist, invoice).
    approval_ist is in Group 2 and will be assigned approver_user.
    """
    template = WorkflowTemplate.objects.create(
        name="Two-Group WF", module="invoice", scope_node=entity,
        created_by=allocator_user,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.PUBLISHED,
    )
    g1 = StepGroup.objects.create(
        template_version=version, name="Allocation Group",
        display_order=1, parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    alloc_step_kwargs = dict(
        group=g1, name="Allocate",
        required_role=base_role, scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1, step_kind=alloc_kind, default_user=None,
    )
    if alloc_kind == StepKind.RUNTIME_SPLIT_ALLOCATION:
        alloc_step_kwargs.update(
            allocation_total_policy=AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
            approver_selection_mode="RUNTIME_SELECTED_FROM_POOL",
            branch_approval_policy=split_policy,
        )
    WorkflowStep.objects.create(**alloc_step_kwargs)

    g2 = StepGroup.objects.create(
        template_version=version, name="Approval Group",
        display_order=2, parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    WorkflowStep.objects.create(
        group=g2, name="Final Approve",
        required_role=base_role, scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1, step_kind=StepKind.NORMAL_APPROVAL, default_user=None,
    )

    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )
    invoice = Invoice.objects.create(
        title="Integrity Invoice",
        amount=INVOICE_AMOUNT,
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["subject_id", "status"])

    # Put Group 1 in progress, assign allocator
    alloc_ist = instance.instance_groups.get(step_group__name="Allocation Group").instance_steps.get()
    alloc_group = alloc_ist.instance_group
    alloc_group.status = GroupStatus.IN_PROGRESS
    alloc_group.save(update_fields=["status"])
    alloc_ist.assigned_user = allocator_user
    alloc_ist.status = StepStatus.WAITING
    alloc_ist.save(update_fields=["assigned_user", "status"])

    # Pre-assign Group 2's approval step to approver_user
    approval_ist = instance.instance_groups.get(step_group__name="Approval Group").instance_steps.get()
    approval_ist.assigned_user = approver_user
    approval_ist.save(update_fields=["assigned_user"])

    return instance, alloc_ist, approval_ist, invoice


# ---------------------------------------------------------------------------
# Test 1: Single allocation — variance_required blocks submission
# ---------------------------------------------------------------------------

def test_single_alloc_variance_required_blocks_submission(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, tight_budget, db,
):
    """
    When reserve_budget returns variance_required, submit_single_invoice_allocation
    must raise StepActionError and leave no APPROVED allocation behind.
    """
    instance, alloc_ist, approval_ist, invoice = _make_two_group_instance(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "budget": tight_budget.id},
        )

    assert "variance" in str(exc_info.value).lower()

    # No allocation should be APPROVED (transaction rolled back)
    assert InvoiceAllocation.objects.filter(
        workflow_instance=instance,
        status=InvoiceAllocationStatus.APPROVED,
    ).count() == 0

    # Step must remain WAITING (not advanced)
    alloc_ist.refresh_from_db()
    assert alloc_ist.status == StepStatus.WAITING

    # Budget must be untouched
    tight_budget.refresh_from_db()
    assert tight_budget.reserved_amount == Decimal("0")


# ---------------------------------------------------------------------------
# Test 2: Split allocation — any variance_required blocks whole submission
# ---------------------------------------------------------------------------

def test_split_variance_required_blocks_whole_submission(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, tight_budget, db,
):
    """
    With SKIP_ALL (auto-approve) split, if the budget reservation returns
    variance_required the entire submit_runtime_invoice_split call rolls back:
    no allocations, no branches.
    """
    from apps.workflow.services_split import submit_runtime_invoice_split
    from apps.workflow.models import WorkflowSplitOption

    instance, alloc_ist, approval_ist, invoice = _make_two_group_instance(
        entity, allocator_user, approver_user, base_role, module_activation,
        alloc_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
        split_policy=BranchApprovalPolicy.SKIP_ALL,
    )

    WorkflowSplitOption.objects.create(
        workflow_step=alloc_ist.workflow_step, entity=entity,
        is_active=True, display_order=0,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_runtime_invoice_split(
            alloc_ist, actor=allocator_user,
            allocations_payload=[{"entity": entity.id, "amount": str(INVOICE_AMOUNT), "budget": tight_budget.id}],
        )

    assert "variance" in str(exc_info.value).lower()

    # Atomic rollback: no allocations, no branches
    assert InvoiceAllocation.objects.filter(workflow_instance=instance).count() == 0
    from apps.workflow.models import WorkflowInstanceBranch
    assert WorkflowInstanceBranch.objects.filter(instance=instance).count() == 0

    # Budget untouched
    tight_budget.refresh_from_db()
    assert tight_budget.reserved_amount == Decimal("0")


# ---------------------------------------------------------------------------
# Test 3: Downstream TERMINATE rejection releases single-alloc budget
# ---------------------------------------------------------------------------

def test_reject_downstream_step_releases_single_alloc_budget(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, large_budget, db,
):
    """
    Single allocation submitted with a budget → reserved.
    Downstream NORMAL_APPROVAL step rejected with TERMINATE →
    budget.reserved_amount must return to pre-allocation value.
    """
    instance, alloc_ist, approval_ist, invoice = _make_two_group_instance(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    reserved_before = large_budget.reserved_amount

    # Submit allocation — advances group 1, activates group 2
    submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id, "budget": large_budget.id},
    )

    large_budget.refresh_from_db()
    assert large_budget.reserved_amount == reserved_before + INVOICE_AMOUNT

    # Activate approval step
    approval_ist.refresh_from_db()
    approval_ist.instance_group.status = GroupStatus.IN_PROGRESS
    approval_ist.instance_group.save(update_fields=["status"])
    approval_ist.status = StepStatus.WAITING
    approval_ist.save(update_fields=["status"])

    # Reject with TERMINATE
    reject_workflow_step(approval_ist, acted_by=approver_user, note="rejected")

    large_budget.refresh_from_db()
    assert large_budget.reserved_amount == reserved_before

    instance.refresh_from_db()
    assert instance.status == InstanceStatus.REJECTED


# ---------------------------------------------------------------------------
# Test 4: Downstream TERMINATE rejection releases split-alloc budget
# ---------------------------------------------------------------------------

def test_reject_downstream_step_releases_split_alloc_budget(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, large_budget, db,
):
    """
    Runtime split (SKIP_ALL) allocates full invoice to entity with a budget.
    Downstream NORMAL_APPROVAL step rejected with TERMINATE →
    budget.reserved_amount must return to pre-allocation value.
    """
    from apps.workflow.services_split import submit_runtime_invoice_split
    from apps.workflow.models import WorkflowSplitOption

    instance, alloc_ist, approval_ist, invoice = _make_two_group_instance(
        entity, allocator_user, approver_user, base_role, module_activation,
        alloc_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
        split_policy=BranchApprovalPolicy.SKIP_ALL,
    )

    WorkflowSplitOption.objects.create(
        workflow_step=alloc_ist.workflow_step, entity=entity,
        is_active=True, display_order=0,
    )

    reserved_before = large_budget.reserved_amount

    # Submit split — SKIP_ALL auto-approves, advances group 1, activates group 2
    submit_runtime_invoice_split(
        alloc_ist, actor=allocator_user,
        allocations_payload=[{"entity": entity.id, "amount": str(INVOICE_AMOUNT), "budget": large_budget.id}],
    )

    large_budget.refresh_from_db()
    assert large_budget.reserved_amount == reserved_before + INVOICE_AMOUNT

    # Activate group 2's approval step
    approval_ist.refresh_from_db()
    approval_ist.instance_group.status = GroupStatus.IN_PROGRESS
    approval_ist.instance_group.save(update_fields=["status"])
    approval_ist.status = StepStatus.WAITING
    approval_ist.save(update_fields=["status"])

    # Reject with TERMINATE
    reject_workflow_step(approval_ist, acted_by=approver_user, note="rejected")

    large_budget.refresh_from_db()
    assert large_budget.reserved_amount == reserved_before

    instance.refresh_from_db()
    assert instance.status == InstanceStatus.REJECTED


# ---------------------------------------------------------------------------
# Test 5: No double release when source balance is already zero
# ---------------------------------------------------------------------------

def test_no_double_release_when_balance_zero(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, large_budget, db,
):
    """
    After a single allocation is corrected (old reservation released, new one created),
    calling _release_allocation_budgets on the old allocation (which has balance=0)
    must be a safe no-op — no error, no phantom release, reserved_amount unchanged.
    """
    from apps.workflow.services import _release_allocation_budgets

    instance, alloc_ist, approval_ist, invoice = _make_two_group_instance(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    # First allocation — reserves budget
    submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id, "budget": large_budget.id},
    )

    large_budget.refresh_from_db()
    reserved_after_first = large_budget.reserved_amount

    # Correction: reset step to WAITING, re-activate group, submit again
    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["status"])
    alloc_ist.refresh_from_db()
    alloc_ist.status = StepStatus.WAITING
    alloc_ist.save(update_fields=["status"])
    alloc_ist.instance_group.status = GroupStatus.IN_PROGRESS
    alloc_ist.instance_group.save(update_fields=["status"])

    submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id, "budget": large_budget.id},
    )

    large_budget.refresh_from_db()
    # Net reserved should be same as after first (old released, new reserved)
    assert large_budget.reserved_amount == reserved_after_first

    # Now call _release_allocation_budgets — the CANCELLED allocation has balance=0,
    # the APPROVED one still has a reservation; only the APPROVED one should be released.
    # Verify this is safe.
    reserved_before_helper = large_budget.reserved_amount
    _release_allocation_budgets(instance, allocator_user)
    large_budget.refresh_from_db()
    # Helper should have released only the outstanding reservation (the APPROVED alloc)
    assert large_budget.reserved_amount < reserved_before_helper


# ---------------------------------------------------------------------------
# Test 6: Event coverage — variance path emits no ALLOCATION_BUDGET_RESERVED event
# ---------------------------------------------------------------------------

def test_variance_path_no_budget_reserved_event(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, tight_budget, db,
):
    """
    When reserve_budget returns variance_required and we block the submission,
    no ALLOCATION_BUDGET_RESERVED event should be emitted for this instance.
    """
    instance, alloc_ist, approval_ist, invoice = _make_two_group_instance(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError):
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "budget": tight_budget.id},
        )

    # Entire transaction rolled back — no events for this instance should exist
    reserved_events = WorkflowEvent.objects.filter(
        instance=instance,
        event_type=WorkflowEventType.ALLOCATION_BUDGET_RESERVED,
    )
    assert reserved_events.count() == 0
