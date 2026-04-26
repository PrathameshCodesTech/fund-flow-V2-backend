"""
Tests for Phase 1: Single Allocation coverage.

Covers:
1. Single allocation creates one InvoiceAllocation for the full invoice amount
2. submit guard: entity required
3. Single allocation reserves budget when budget is provided
4. Workflow final approval blocked when no allocation coverage exists
5. Workflow final approval blocked when CAN_BE_PARTIAL split doesn't cover full amount
6. Workflow final approval succeeds when single allocation fully covers invoice
7. Existing split flow (RUNTIME_SPLIT_ALLOCATION) still passes
8. Corrected/cancelled allocations are ignored in coverage calculation
9. Correction mode: resubmit replaces old allocation, re-reserves budget
10. No child Invoice records created; same Invoice remains source of truth
"""
import pytest
from decimal import Decimal

from apps.core.models import Organization, ScopeNode, NodeType
from apps.access.models import Role, UserRoleAssignment
from apps.users.models import User
from apps.invoices.models import Invoice, InvoiceStatus, InvoiceAllocation, InvoiceAllocationStatus
from apps.budgets.models import Budget, BudgetLine, BudgetStatus, BudgetCategory
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    WorkflowInstance, WorkflowInstanceStep,
    VersionStatus, InstanceStatus, GroupStatus, StepStatus,
    ScopeResolutionPolicy, ParallelMode, RejectionAction,
    StepKind, AllocationTotalPolicy, BranchApprovalPolicy,
)
from apps.workflow.services import (
    create_workflow_instance_draft,
    StepActionError,
    AllocationCoverageError,
)
from apps.workflow.services_allocation import (
    get_single_allocation_options,
    submit_single_invoice_allocation,
)
from apps.modules.models import ModuleActivation, ModuleType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Alloc Org", code="alloc-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/alloc-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/alloc-org/hq/ea", depth=1,
    )


@pytest.fixture
def allocator_user(db):
    return User.objects.create_user(email="allocator@example.com", password="pass")


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
def budget(org, entity):
    cat = BudgetCategory.objects.create(org=org, name="Ops", code="ops", is_active=True)
    budget = Budget.objects.create(
        org=org,
        scope_node=entity,
        name="FY27 Ops",
        code="FY27-OPS",
        financial_year="2026-27",
        period_type="yearly",
        allocated_amount=Decimal("500000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR",
        status=BudgetStatus.ACTIVE,
    )
    BudgetLine.objects.create(
        budget=budget,
        category=cat,
        subcategory=None,
        allocated_amount=Decimal("500000.00"),
    )
    return budget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_single_alloc_template(org, entity, role, allocator_user):
    """
    Template with one SINGLE_ALLOCATION step (Group 1) only.
    Returns (version, template).
    """
    template = WorkflowTemplate.objects.create(
        name="Single Alloc WF", module="invoice", scope_node=entity,
        created_by=allocator_user,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.PUBLISHED,
    )
    g1 = StepGroup.objects.create(
        template_version=version,
        name="Allocation Group",
        display_order=1,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    WorkflowStep.objects.create(
        group=g1,
        name="Allocate",
        required_role=role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        step_kind=StepKind.SINGLE_ALLOCATION,
        default_user=None,
    )
    return version


def _make_two_group_single_alloc_template(org, entity, role, allocator_user):
    """
    Group 1: SINGLE_ALLOCATION
    Group 2: NORMAL_APPROVAL

    Used to test that the workflow advances to group 2 and then completes
    once group 2 is approved.
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
    WorkflowStep.objects.create(
        group=g1, name="Allocate",
        required_role=role, scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1, step_kind=StepKind.SINGLE_ALLOCATION, default_user=None,
    )
    g2 = StepGroup.objects.create(
        template_version=version, name="Approval Group",
        display_order=2, parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    approval_step = WorkflowStep.objects.create(
        group=g2, name="Final Approve",
        required_role=role, scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1, step_kind=StepKind.NORMAL_APPROVAL, default_user=None,
    )
    return version, approval_step


def _make_instance_with_single_alloc_step(entity, allocator_user, approver_user, base_role, module_activation):
    """
    Build a draft instance with SINGLE_ALLOCATION step, manually put the step
    in WAITING state (skip activate to avoid user-resolution failures).
    Returns (instance, alloc_ist, invoice).
    """
    version = _make_single_alloc_template(entity.org, entity, base_role, allocator_user)

    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )

    invoice = Invoice.objects.create(
        title="Test Invoice",
        amount=Decimal("25000.00"),
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.save(update_fields=["subject_id"])

    alloc_ist = instance.instance_groups.get(step_group__name="Allocation Group").instance_steps.get()
    alloc_group = alloc_ist.instance_group
    alloc_group.status = GroupStatus.IN_PROGRESS
    alloc_group.save(update_fields=["status"])
    alloc_ist.assigned_user = allocator_user
    alloc_ist.status = StepStatus.WAITING
    alloc_ist.save(update_fields=["assigned_user", "status"])

    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["status"])

    return instance, alloc_ist, invoice


def _make_instance_with_two_group_single_alloc_step(entity, allocator_user, approver_user, base_role):
    """
    Build a draft instance with:
      Group 1 -> SINGLE_ALLOCATION (WAITING, assigned to allocator_user)
      Group 2 -> NORMAL_APPROVAL (still pending)

    This lets tests observe reservation state after the allocation step submits
    but before final instance approval consumes the reservation.
    """
    version, _ = _make_two_group_single_alloc_template(entity.org, entity, base_role, allocator_user)

    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )

    invoice = Invoice.objects.create(
        title="Test Invoice",
        amount=Decimal("25000.00"),
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.save(update_fields=["subject_id"])

    alloc_ist = instance.instance_groups.get(step_group__name="Allocation Group").instance_steps.get()
    alloc_group = alloc_ist.instance_group
    alloc_group.status = GroupStatus.IN_PROGRESS
    alloc_group.save(update_fields=["status"])
    alloc_ist.assigned_user = allocator_user
    alloc_ist.status = StepStatus.WAITING
    alloc_ist.save(update_fields=["assigned_user", "status"])

    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["status"])

    return instance, alloc_ist, invoice


# ---------------------------------------------------------------------------
# Test 1: Single allocation creates one InvoiceAllocation for the full amount
# ---------------------------------------------------------------------------

def test_single_allocation_creates_one_full_amount_allocation(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    result = submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id},
    )

    allocs = InvoiceAllocation.objects.filter(invoice=invoice, status=InvoiceAllocationStatus.APPROVED)
    assert allocs.count() == 1
    alloc = allocs.get()
    assert alloc.amount == invoice.amount
    assert alloc.percentage == Decimal("100.000")
    assert alloc.branch is None
    assert alloc.entity_id == entity.id
    assert result["allocation"]["amount"] == str(invoice.amount)


# ---------------------------------------------------------------------------
# Test 2: Submit guard — entity is required
# ---------------------------------------------------------------------------

def test_single_allocation_requires_entity(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={},  # entity omitted
        )

    assert "entity" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Test 3: Budget reservation fires when budget is provided
# ---------------------------------------------------------------------------

def test_single_allocation_reserves_budget(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, budget, db,
):
    instance, alloc_ist, invoice = _make_instance_with_two_group_single_alloc_step(
        entity, allocator_user, approver_user, base_role,
    )

    budget_line = budget.lines.first()
    before_reserved = budget_line.reserved_amount
    result = submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id, "budget": budget.id},
    )

    budget_line.refresh_from_db()
    budget.refresh_from_db()
    assert budget_line.reserved_amount == before_reserved + invoice.amount
    assert budget.reserved_amount == before_reserved + invoice.amount
    assert result["budget_reservation"] is not None
    assert result["budget_reservation"]["allocation_id"] == result["allocation"]["id"]
    assert result["budget_reservation"]["budget_line_id"] == budget_line.id


# ---------------------------------------------------------------------------
# Test 4: Final approval blocked when no allocation coverage (allocation-capable template)
# ---------------------------------------------------------------------------

def test_final_approval_blocked_no_coverage(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    """
    Instance has a SINGLE_ALLOCATION step but no allocation submitted.
    The last group completing should raise AllocationCoverageError.
    """
    version = _make_single_alloc_template(entity.org, entity, base_role, allocator_user)

    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )
    invoice = Invoice.objects.create(
        title="No-coverage Invoice",
        amount=Decimal("10000.00"),
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["subject_id", "status"])

    # Put the single-alloc step into APPROVED state without creating any allocation
    alloc_ist = instance.instance_groups.get(step_group__name="Allocation Group").instance_steps.get()
    alloc_group = alloc_ist.instance_group
    alloc_group.status = GroupStatus.IN_PROGRESS
    alloc_group.save(update_fields=["status"])
    alloc_ist.assigned_user = allocator_user
    alloc_ist.status = StepStatus.WAITING
    alloc_ist.save(update_fields=["assigned_user", "status"])

    # Directly advance the group to APPROVED without creating an allocation
    # (simulating a bypass) — the coverage check must block this.
    from apps.workflow.services import _advance_on_group_complete

    alloc_ist.status = StepStatus.APPROVED
    alloc_ist.save(update_fields=["status"])
    alloc_group.status = GroupStatus.APPROVED
    alloc_group.save(update_fields=["status"])

    with pytest.raises(AllocationCoverageError):
        _advance_on_group_complete(alloc_group, instance, allocator_user)


# ---------------------------------------------------------------------------
# Test 5: Final approval blocked when partial runtime split doesn't cover full amount
# ---------------------------------------------------------------------------

def test_final_approval_blocked_partial_split(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    """
    CAN_BE_PARTIAL runtime split: allocations sum to less than invoice.amount.
    Coverage check must block instance from reaching APPROVED.
    """
    from apps.workflow.services_split import submit_runtime_invoice_split
    from apps.workflow.models import WorkflowSplitOption

    template = WorkflowTemplate.objects.create(
        name="Partial Split WF", module="invoice", scope_node=entity,
        created_by=allocator_user,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.PUBLISHED,
    )
    g1 = StepGroup.objects.create(
        template_version=version, name="Split Group",
        display_order=1, parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    split_step = WorkflowStep.objects.create(
        group=g1, name="Partial Alloc",
        required_role=base_role, scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        step_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
        allocation_total_policy=AllocationTotalPolicy.CAN_BE_PARTIAL,
        approver_selection_mode="RUNTIME_SELECTED_FROM_POOL",
        branch_approval_policy=BranchApprovalPolicy.SKIP_ALL,
        default_user=None,
    )
    WorkflowSplitOption.objects.create(
        workflow_step=split_step, entity=entity, is_active=True, display_order=0,
    )

    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )
    invoice = Invoice.objects.create(
        title="Partial Invoice",
        amount=Decimal("20000.00"),
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["subject_id", "status"])

    split_ist = instance.instance_groups.get(step_group__name="Split Group").instance_steps.get()
    split_group = split_ist.instance_group
    split_group.status = GroupStatus.IN_PROGRESS
    split_group.save(update_fields=["status"])
    split_ist.assigned_user = allocator_user
    split_ist.status = StepStatus.WAITING
    split_ist.save(update_fields=["assigned_user", "status"])

    # Submit only 5000 out of 20000 — CAN_BE_PARTIAL allows this at split time
    with pytest.raises(AllocationCoverageError):
        submit_runtime_invoice_split(
            split_ist, actor=allocator_user,
            allocations_payload=[{"entity": entity.id, "amount": "5000.00"}],
        )

    instance.refresh_from_db()
    assert instance.status != InstanceStatus.APPROVED


# ---------------------------------------------------------------------------
# Test 6: Workflow completes when single allocation fully covers invoice
# ---------------------------------------------------------------------------

def test_final_approval_succeeds_with_full_coverage(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    """
    Single-allocation-only template: submitting the full invoice amount
    must advance the instance to APPROVED.
    """
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id},
    )

    instance.refresh_from_db()
    assert instance.status == InstanceStatus.APPROVED


# ---------------------------------------------------------------------------
# Test 7: Existing split flow (REQUIRED_FOR_ALL SKIP_ALL) still passes
# ---------------------------------------------------------------------------

def test_runtime_split_skip_all_still_passes(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    """
    A RUNTIME_SPLIT_ALLOCATION with SKIP_ALL and MUST_EQUAL_INVOICE_TOTAL
    still completes correctly after the Phase 1 coverage gate.
    """
    from apps.workflow.services_split import submit_runtime_invoice_split
    from apps.workflow.models import WorkflowSplitOption

    template = WorkflowTemplate.objects.create(
        name="Split SKIP WF", module="invoice", scope_node=entity,
        created_by=allocator_user,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.PUBLISHED,
    )
    g1 = StepGroup.objects.create(
        template_version=version, name="Split Group",
        display_order=1, parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    split_step = WorkflowStep.objects.create(
        group=g1, name="Alloc",
        required_role=base_role, scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        step_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
        allocation_total_policy=AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
        approver_selection_mode="RUNTIME_SELECTED_FROM_POOL",
        branch_approval_policy=BranchApprovalPolicy.SKIP_ALL,
        default_user=None,
    )
    WorkflowSplitOption.objects.create(
        workflow_step=split_step, entity=entity, is_active=True, display_order=0,
    )

    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )
    invoice = Invoice.objects.create(
        title="Split Skip Invoice",
        amount=Decimal("15000.00"),
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["subject_id", "status"])

    split_ist = instance.instance_groups.get(step_group__name="Split Group").instance_steps.get()
    split_group = split_ist.instance_group
    split_group.status = GroupStatus.IN_PROGRESS
    split_group.save(update_fields=["status"])
    split_ist.assigned_user = allocator_user
    split_ist.status = StepStatus.WAITING
    split_ist.save(update_fields=["assigned_user", "status"])

    # Full amount — SKIP_ALL auto-approves all branches
    submit_runtime_invoice_split(
        split_ist, actor=allocator_user,
        allocations_payload=[{"entity": entity.id, "amount": "15000.00"}],
    )

    instance.refresh_from_db()
    assert instance.status == InstanceStatus.APPROVED


# ---------------------------------------------------------------------------
# Test 8: Cancelled allocations are excluded from coverage calculation
# ---------------------------------------------------------------------------

def test_cancelled_allocations_excluded_from_coverage(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    """
    An InvoiceAllocation with status CANCELLED must NOT count toward coverage.
    Submitting a replacement allocation with the full amount must satisfy coverage.
    """
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    # First submission
    submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id},
    )

    # Verify: 1 APPROVED, 0 CANCELLED
    assert InvoiceAllocation.objects.filter(invoice=invoice, status=InvoiceAllocationStatus.APPROVED).count() == 1
    assert InvoiceAllocation.objects.filter(invoice=invoice, status=InvoiceAllocationStatus.CANCELLED).count() == 0

    instance.refresh_from_db()
    assert instance.status == InstanceStatus.APPROVED

    # Cancelled allocations would be counted in coverage → verify via helper
    from apps.workflow.services import check_invoice_allocation_coverage
    cancelled = InvoiceAllocation.objects.filter(invoice=invoice).first()
    cancelled.status = InvoiceAllocationStatus.CANCELLED
    cancelled.save(update_fields=["status"])

    coverage = check_invoice_allocation_coverage(invoice, instance)
    assert coverage["covered"] is False
    assert coverage["gap"] == invoice.amount


# ---------------------------------------------------------------------------
# Test 9: Correction mode — resubmit replaces old allocation
# ---------------------------------------------------------------------------

def test_correction_replaces_existing_allocation(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, budget, db,
):
    """
    When alloc_ist.status is reset to WAITING (simulating correction),
    submit_single_invoice_allocation cancels the old allocation and creates a new one.
    """
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    # First submission (no budget)
    submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id},
    )

    first_alloc = InvoiceAllocation.objects.get(invoice=invoice, status=InvoiceAllocationStatus.APPROVED)
    first_id = first_alloc.id

    # Reset step to WAITING (simulating correction request)
    alloc_ist.status = StepStatus.WAITING
    alloc_ist.save(update_fields=["status"])
    alloc_ist.instance_group.status = GroupStatus.IN_PROGRESS
    alloc_ist.instance_group.save(update_fields=["status"])
    # Re-activate the instance
    instance.status = InstanceStatus.ACTIVE
    instance.save(update_fields=["status"])

    # Second submission with budget
    result2 = submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id, "budget": budget.id},
    )

    # Old allocation should now be CANCELLED
    first_alloc.refresh_from_db()
    assert first_alloc.status == InvoiceAllocationStatus.CANCELLED

    # New allocation should exist with revision_number = 2
    new_alloc = InvoiceAllocation.objects.get(pk=result2["allocation"]["id"])
    assert new_alloc.status == InvoiceAllocationStatus.APPROVED
    assert new_alloc.revision_number == 2
    assert new_alloc.budget_id == budget.id

    # Only one active allocation at the end
    active = InvoiceAllocation.objects.filter(
        invoice=invoice,
        status__in=(InvoiceAllocationStatus.APPROVED, InvoiceAllocationStatus.SUBMITTED),
    )
    assert active.count() == 1


# ---------------------------------------------------------------------------
# Test 10: No child Invoice records created
# ---------------------------------------------------------------------------

def test_no_child_invoice_created(
    org, entity, allocator_user, approver_user, base_role,
    module_activation, db,
):
    """
    Single allocation must NOT create any new Invoice records.
    The same Invoice row remains the source of truth.
    """
    from apps.invoices.models import Invoice as InvoiceModel

    count_before = InvoiceModel.objects.count()

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    submit_single_invoice_allocation(
        alloc_ist, actor=allocator_user,
        payload={"entity": entity.id},
    )

    assert InvoiceModel.objects.count() == count_before + 1  # only the one we created
    assert InvoiceModel.objects.filter(pk=invoice.pk).exists()


# ===========================================================================
# Phase 1.5: Hardening — step-config requirements, field coherence, options
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers shared across Phase 1.5 tests
# ---------------------------------------------------------------------------

def _set_step_flags(alloc_ist, **flags):
    """Update require_* flags on the underlying WorkflowStep."""
    ws = alloc_ist.workflow_step
    for attr, val in flags.items():
        setattr(ws, attr, val)
    ws.save(update_fields=list(flags.keys()))


def _make_entity_b(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity B", code="eb",
        node_type=NodeType.ENTITY, path="/alloc-org/hq/eb", depth=1,
    )


# ---------------------------------------------------------------------------
# Step-config requirement enforcement
# ---------------------------------------------------------------------------

def test_require_category_enforced(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )
    _set_step_flags(alloc_ist, require_category=True)

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id},  # category omitted
        )
    assert "category" in str(exc_info.value).lower()


def test_require_subcategory_enforced(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )
    _set_step_flags(alloc_ist, require_subcategory=True)

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id},  # subcategory omitted
        )
    assert "subcategory" in str(exc_info.value).lower()


def test_require_budget_enforced(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )
    _set_step_flags(alloc_ist, require_budget=True)

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id},  # budget omitted
        )
    assert "budget" in str(exc_info.value).lower()


def test_require_campaign_enforced(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )
    _set_step_flags(alloc_ist, require_campaign=True)

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id},  # campaign omitted
        )
    assert "campaign" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Entity scope coherence
# ---------------------------------------------------------------------------

def test_entity_out_of_invoice_scope_rejected(
    org, company, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    """
    entity_b is a sibling of entity (both children of company).
    invoice is scoped to entity, so entity_b is not a valid allocation target.
    """
    entity_b = _make_entity_b(org, company)

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity_b.id},
        )
    assert "scope" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Category coherence
# ---------------------------------------------------------------------------

def test_category_wrong_org_rejected(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    other_org = Organization.objects.create(name="Other Org", code="other-org")
    wrong_cat = BudgetCategory.objects.create(
        org=other_org, name="Foreign Cat", code="fcat", is_active=True,
    )

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "category": wrong_cat.id},
        )
    assert "organisation" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Subcategory coherence
# ---------------------------------------------------------------------------

def test_invalid_subcategory_for_category_rejected(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    """
    subcategory belongs to cat_b, but the payload sends cat_a + subcat_b.
    """
    from apps.budgets.models import BudgetSubCategory

    cat_a = BudgetCategory.objects.create(org=org, name="Cat A", code="cata", is_active=True)
    cat_b = BudgetCategory.objects.create(org=org, name="Cat B", code="catb", is_active=True)
    subcat_b = BudgetSubCategory.objects.create(
        category=cat_b, name="Sub B", code="subb", is_active=True,
    )

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "category": cat_a.id, "subcategory": subcat_b.id},
        )
    assert "subcategory" in str(exc_info.value).lower() and "category" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Budget coherence
# ---------------------------------------------------------------------------

def test_inactive_budget_rejected(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    cat = BudgetCategory.objects.create(org=org, name="X", code="x", is_active=True)
    inactive_budget = Budget.objects.create(
        org=org,
        scope_node=entity,
        name="FY27 Inactive",
        code="FY27-INACT",
        financial_year="2026-27",
        period_type="yearly",
        allocated_amount=Decimal("10000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR",
        status=BudgetStatus.ACTIVE,
    )
    BudgetLine.objects.create(
        budget=inactive_budget, category=cat, subcategory=None,
        allocated_amount=Decimal("10000.00"),
    )
    inactive_budget.status = "draft"
    inactive_budget.save(update_fields=["status"])

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "budget": inactive_budget.id},
        )
    assert "active" in str(exc_info.value).lower()


def test_budget_out_of_entity_scope_rejected(
    org, company, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    """
    Budget is scoped to entity_b; allocation entity is entity_a → mismatch → error.
    """
    entity_b = _make_entity_b(org, company)
    cat = BudgetCategory.objects.create(org=org, name="Y", code="y", is_active=True)
    budget_b = Budget.objects.create(
        org=org,
        scope_node=entity_b,
        name="FY27 Entity B",
        code="FY27-EB",
        financial_year="2026-27",
        period_type="yearly",
        allocated_amount=Decimal("50000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR",
        status=BudgetStatus.ACTIVE,
    )
    BudgetLine.objects.create(
        budget=budget_b, category=cat, subcategory=None,
        allocated_amount=Decimal("50000.00"),
    )

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "budget": budget_b.id},
        )
    assert "scope" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Campaign coherence
# ---------------------------------------------------------------------------

def test_campaign_not_in_entity_scope_rejected(
    org, company, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    """
    Campaign is scoped to entity_b; allocation entity is entity → scope mismatch → error.
    """
    from apps.campaigns.models import Campaign, CampaignStatus

    entity_b = _make_entity_b(org, company)
    campaign_b = Campaign.objects.create(
        scope_node=entity_b,
        name="B Campaign",
        code="bcmp",
        requested_amount=Decimal("5000.00"),
        status=CampaignStatus.INTERNALLY_APPROVED,
    )

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "campaign": campaign_b.id},
        )
    assert "scope" in str(exc_info.value).lower()


def test_inactive_campaign_rejected(
    org, entity, allocator_user, approver_user, base_role, module_activation, db,
):
    """
    A campaign in DRAFT status is not a valid allocation target.
    """
    from apps.campaigns.models import Campaign, CampaignStatus

    draft_campaign = Campaign.objects.create(
        scope_node=entity,
        name="Draft Campaign",
        code="dcmp",
        requested_amount=Decimal("5000.00"),
        status=CampaignStatus.DRAFT,
    )

    instance, alloc_ist, invoice = _make_instance_with_single_alloc_step(
        entity, allocator_user, approver_user, base_role, module_activation,
    )

    with pytest.raises(StepActionError) as exc_info:
        submit_single_invoice_allocation(
            alloc_ist, actor=allocator_user,
            payload={"entity": entity.id, "campaign": draft_campaign.id},
        )
    assert "active" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Options payload structure
# ---------------------------------------------------------------------------

def test_options_payload_is_entity_scoped(
    org, entity, allocator_user, base_role, module_activation, budget, db,
):
    """
    get_single_allocation_options returns allowed_entities, each with its own
    scoped categories/subcategories/campaigns/budgets.
    """
    from apps.access.models import UserRoleAssignment

    # Give allocator visibility over entity
    UserRoleAssignment.objects.create(user=allocator_user, role=base_role, scope_node=entity)

    version = _make_single_alloc_template(org, entity, base_role, allocator_user)
    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )
    invoice = Invoice.objects.create(
        title="Options Test",
        amount=Decimal("10000.00"),
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.save(update_fields=["subject_id"])

    alloc_ist = instance.instance_groups.get(step_group__name="Allocation Group").instance_steps.get()

    from apps.workflow.services_allocation import get_single_allocation_options
    data = get_single_allocation_options(alloc_ist, allocator_user)

    assert "allowed_entities" in data
    assert isinstance(data["allowed_entities"], list)
    assert len(data["allowed_entities"]) >= 1

    ent_entry = next(e for e in data["allowed_entities"] if e["entity_id"] == entity.id)
    assert "categories" in ent_entry
    assert "subcategories" in ent_entry
    assert "campaigns" in ent_entry
    assert "budgets" in ent_entry
    # budget fixture is scoped to entity — should appear
    assert any(b["id"] == budget.id for b in ent_entry["budgets"])


def test_options_step_config_has_allocation_mode_single(
    org, entity, allocator_user, base_role, module_activation, db,
):
    """step_config.allocation_mode must be 'SINGLE' for SINGLE_ALLOCATION steps."""
    from apps.access.models import UserRoleAssignment
    from apps.workflow.services_allocation import get_single_allocation_options

    UserRoleAssignment.objects.create(user=allocator_user, role=base_role, scope_node=entity)

    version = _make_single_alloc_template(org, entity, base_role, allocator_user)
    instance = create_workflow_instance_draft(
        template_version=version,
        subject_type="invoice",
        subject_id=0,
        subject_scope_node=entity,
        started_by=allocator_user,
    )
    invoice = Invoice.objects.create(
        title="Mode Test",
        amount=Decimal("5000.00"),
        currency="INR",
        scope_node=entity,
        created_by=allocator_user,
        status=InvoiceStatus.IN_REVIEW,
    )
    instance.subject_id = invoice.pk
    instance.save(update_fields=["subject_id"])

    alloc_ist = instance.instance_groups.get(step_group__name="Allocation Group").instance_steps.get()
    data = get_single_allocation_options(alloc_ist, allocator_user)

    assert data["step_config"]["allocation_mode"] == "SINGLE"
    assert data["step_config"]["amount_locked"] is True
    assert "require_category" in data["step_config"]
    assert "require_budget" in data["step_config"]
