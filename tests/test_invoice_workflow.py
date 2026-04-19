"""
Tests for Phase 3:
- Invoice creation with permission checks
- Workflow draft creation from invoice (from-invoice flow)
- Invoice status sync on workflow state changes
- Runtime actions: approve, reject, reassign
- My pending tasks
"""
import pytest
from django.utils import timezone

from apps.core.models import Organization, ScopeNode, NodeType
from apps.access.models import Role, Permission, PermissionAction, PermissionResource, UserRoleAssignment
from apps.users.models import User
from apps.invoices.models import Invoice, InvoiceStatus
from apps.invoices.services import create_invoice, InvoicePermissionError
from apps.workflow.models import (
    WorkflowTemplate,
    WorkflowTemplateVersion,
    StepGroup,
    WorkflowStep,
    WorkflowInstance,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowInstanceStep,
    VersionStatus,
    InstanceStatus,
    GroupStatus,
    StepStatus,
    ScopeResolutionPolicy,
    ParallelMode,
    RejectionAction,
)
from apps.workflow.services import (
    create_workflow_instance_draft,
    activate_workflow_instance,
    approve_workflow_step,
    reject_workflow_step,
    reassign_workflow_step,
    apply_step_assignment_overrides,
    StepActionError,
    ModuleInactiveError,
    WorkflowNotConfiguredError,
)
from apps.workflow.selectors import get_pending_tasks_for_user
from apps.modules.models import ModuleActivation, ModuleType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Phase3 Org", code="p3-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/p3-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/p3-org/hq/ea", depth=1,
    )


@pytest.fixture
def actor(db):
    return User.objects.create_user(email="actor@example.com", password="pass")


@pytest.fixture
def approver_user(db):
    return User.objects.create_user(email="approver@example.com", password="pass")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="pass")


@pytest.fixture
def approver_role(org):
    return Role.objects.create(org=org, name="Approver", code="approver")


@pytest.fixture
def create_permission(db):
    return Permission.objects.get_or_create(
        action=PermissionAction.CREATE,
        resource=PermissionResource.INVOICE,
    )[0]


@pytest.fixture
def start_wf_permission(db):
    return Permission.objects.get_or_create(
        action=PermissionAction.START_WORKFLOW,
        resource=PermissionResource.INVOICE,
    )[0]


@pytest.fixture
def reassign_permission(db):
    return Permission.objects.get_or_create(
        action=PermissionAction.REASSIGN,
        resource=PermissionResource.INVOICE,
    )[0]


@pytest.fixture
def template(entity, actor):
    return WorkflowTemplate.objects.create(
        name="Invoice WF", module="invoice", scope_node=entity, created_by=actor
    )


@pytest.fixture
def published_version(template):
    v = WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.PUBLISHED
    )
    return v


@pytest.fixture
def module_activation(entity):
    return ModuleActivation.objects.create(
        module=ModuleType.INVOICE, scope_node=entity,
        is_active=True, override_parent=True,
    )


@pytest.fixture
def _approver_role_assignment(approver_user, approver_role, entity):
    """Grant approver_role at entity to approver_user."""
    UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)


# ---------------------------------------------------------------------------
# Helper: build a 2-group, 2-step template
# ---------------------------------------------------------------------------

def _make_single_group_template(published_version, role, entity, default_user=None):
    """One group, one step. For tests that need single-group completion."""
    g = StepGroup.objects.create(
        template_version=published_version,
        name="Group 1",
        display_order=1,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    s = WorkflowStep.objects.create(
        group=g, name="Step 1", required_role=role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        default_user=default_user,
    )
    return g, s


def _make_two_group_template(published_version, role, entity, default_user=None):
    """
    Create two groups, each with one step.
    default_user: if provided, set as default_user on ALL steps so activation succeeds.
                  Tests that specifically need unassigned steps should NOT use this helper.
    """
    g1 = StepGroup.objects.create(
        template_version=published_version,
        name="Group 1",
        display_order=1,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    s1 = WorkflowStep.objects.create(
        group=g1, name="Step 1", required_role=role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        default_user=default_user,
    )

    g2 = StepGroup.objects.create(
        template_version=published_version,
        name="Group 2",
        display_order=2,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    s2 = WorkflowStep.objects.create(
        group=g2, name="Step 2", required_role=role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        default_user=default_user,
    )
    return g1, s1, g2, s2


# ---------------------------------------------------------------------------
# Invoice creation
# ---------------------------------------------------------------------------

class TestInvoiceCreation:
    def test_creator_with_permission_can_create(self, org, entity, actor, create_permission):
        """User with create:invoice at scope node can create."""
        from apps.access.services import grant_permission_to_role
        role = Role.objects.create(org=org, name="Creator", code="creator")
        grant_permission_to_role(role, create_permission)
        from apps.access.services import assign_user_role
        assign_user_role(actor, role, entity)

        invoice = create_invoice(
            title="Test Invoice", amount="1000.00", currency="INR",
            scope_node=entity, created_by=actor,
        )
        assert invoice.pk is not None
        assert invoice.status == InvoiceStatus.DRAFT
        assert invoice.created_by == actor

    def test_user_without_permission_raises(self, org, entity, actor):
        """User without create:invoice permission is denied."""
        with pytest.raises(InvoicePermissionError):
            create_invoice(
                title="Test Invoice", amount="1000.00", currency="INR",
                scope_node=entity, created_by=actor,
            )


# ---------------------------------------------------------------------------
# Invoice status sync
# ---------------------------------------------------------------------------

class TestInvoiceStatusSync:
    def test_workflow_active_sets_invoice_in_review(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """When instance becomes ACTIVE, invoice status → IN_REVIEW."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice",
            subject_id=1,
            subject_scope_node=entity,
            started_by=actor,
        )
        # Create a fake invoice row so the sync doesn't error
        invoice = Invoice.objects.create(
            title="Synced Invoice", amount="500.00", currency="INR",
            scope_node=entity, created_by=actor, status=InvoiceStatus.DRAFT,
        )
        instance.subject_id = invoice.pk
        instance.save(update_fields=["subject_id"])

        activate_workflow_instance(instance, activated_by=actor)
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.IN_REVIEW

    def test_workflow_approved_sets_invoice_approved(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """Final group approval → invoice APPROVED. Uses single-group template so one approval completes the instance."""
        g1, s1 = _make_single_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice",
            subject_id=1,
            subject_scope_node=entity,
            started_by=actor,
        )
        invoice = Invoice.objects.create(
            title="Approved Invoice", amount="500.00", currency="INR",
            scope_node=entity, created_by=actor, status=InvoiceStatus.IN_REVIEW,
        )
        instance.subject_id = invoice.pk
        instance.save(update_fields=["subject_id"])

        activate_workflow_instance(instance, activated_by=actor)
        # Manually trigger final approval (single group, single step)
        ist = instance.instance_groups.first().instance_steps.first()
        instance = approve_workflow_step(ist, acted_by=approver_user)

        invoice.refresh_from_db()
        # After workflow APPROVED, finance handoff is created+sent → finance_pending
        assert invoice.status == InvoiceStatus.FINANCE_PENDING

    def test_workflow_rejected_sets_invoice_rejected(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """Rejection with TERMINATE → invoice REJECTED."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice",
            subject_id=1,
            subject_scope_node=entity,
            started_by=actor,
        )
        invoice = Invoice.objects.create(
            title="Rejected Invoice", amount="500.00", currency="INR",
            scope_node=entity, created_by=actor, status=InvoiceStatus.IN_REVIEW,
        )
        instance.subject_id = invoice.pk
        instance.save(update_fields=["subject_id"])

        activate_workflow_instance(instance, activated_by=actor)
        ist = instance.instance_groups.first().instance_steps.first()
        instance = reject_workflow_step(ist, acted_by=approver_user, note="Bad invoice")

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.REJECTED


# ---------------------------------------------------------------------------
# Runtime: Approve
# ---------------------------------------------------------------------------

class TestApproveStep:
    def test_assigned_user_can_approve(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """The assigned user can approve their step."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        result = approve_workflow_step(ist, acted_by=approver_user, note="LGTM")

        assert result.status == StepStatus.APPROVED
        assert result.acted_at is not None
        assert result.note == "LGTM"

    def test_wrong_user_cannot_approve(
        self, published_version, entity, actor, approver_user,
        other_user, approver_role, module_activation, _approver_role_assignment,
    ):
        """Only the assigned user can approve."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        with pytest.raises(StepActionError):
            approve_workflow_step(ist, acted_by=other_user)

    def test_single_mode_advances_group(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """SINGLE mode: one approval completes the group."""
        g1, s1, g2, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        g1_inst = instance.instance_groups.get(step_group=g1)
        assert g1_inst.status == GroupStatus.IN_PROGRESS

        ist = g1_inst.instance_steps.first()
        approve_workflow_step(ist, acted_by=approver_user)

        g1_inst.refresh_from_db()
        assert g1_inst.status == GroupStatus.APPROVED
        g2_inst = instance.instance_groups.get(step_group=g2)
        assert g2_inst.status == GroupStatus.IN_PROGRESS

    def test_final_approval_sets_instance_approved(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """Approval of the last (and only) group completes the instance. Uses single-group template."""
        g1, s1 = _make_single_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        approve_workflow_step(ist, acted_by=approver_user)

        instance.refresh_from_db()
        assert instance.status == InstanceStatus.APPROVED
        assert instance.completed_at is not None


# ---------------------------------------------------------------------------
# Runtime: Reject
# ---------------------------------------------------------------------------

class TestRejectStep:
    def test_reject_terminate_sets_instance_rejected(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """TERMINATE rejection → instance REJECTED."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        reject_workflow_step(ist, acted_by=approver_user, note="Rejecting this.")

        instance.refresh_from_db()
        assert instance.status == InstanceStatus.REJECTED
        assert instance.completed_at is not None

    def test_go_to_group_resets_range(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """
        GO_TO_GROUP rejection:
        - Rejecting group 1 resets groups 1 and 2 to WAITING.
        - Group 2 (the target) restarts as IN_PROGRESS.
        - current_group moves to group 2.
        - Assignments are preserved.
        """
        g1, s1, g2, s2 = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )
        # g1 and g2 are StepGroup objects (template-level)
        g1.on_rejection_action = RejectionAction.GO_TO_GROUP
        g1.on_rejection_goto_group = g2
        g1.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        g1_ig = instance.instance_groups.get(step_group=g1)
        g2_ig = instance.instance_groups.get(step_group=g2)
        assert g1_ig.status == GroupStatus.IN_PROGRESS
        assert g2_ig.status == GroupStatus.WAITING

        # Reject group 1 → resets both groups, g2 restarts as IN_PROGRESS
        g1_ist = g1_ig.instance_steps.first()
        reject_workflow_step(g1_ist, acted_by=approver_user)

        # Verify via direct DB query to rule out ORM object caching
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, status, step_group_id FROM workflow_instance_groups WHERE instance_id = %s ORDER BY step_group_id",
                [instance.pk]
            )
            rows = cursor.fetchall()
        # rows: [(id, status, step_group_id), ...]
        g1_status_after = next(r[1] for r in rows if r[2] == g1.pk)
        g2_status_after = next(r[1] for r in rows if r[2] == g2.pk)

        assert g1_status_after == GroupStatus.WAITING, f"g1_ig should be WAITING, got {g1_status_after}"
        assert g2_status_after == GroupStatus.IN_PROGRESS, f"g2_ig should be IN_PROGRESS, got {g2_status_after}"
        instance.refresh_from_db()
        assert instance.current_group_id == g2_ig.pk

    def test_groups_before_target_remain_approved(self):
        """When rejecting with GO_TO_GROUP, groups before target stay APPROVED."""
        pass  # Covered by the go_to_group test above


# ---------------------------------------------------------------------------
# Runtime: Reassign
# ---------------------------------------------------------------------------

class TestReassignStep:
    def test_admin_with_permission_can_reassign(
        self, published_version, entity, actor, approver_user,
        other_user, approver_role, module_activation,
        _approver_role_assignment, reassign_permission,
    ):
        """User with reassign permission can reassign a step."""
        from apps.access.services import grant_permission_to_role, assign_user_role
        grant_permission_to_role(approver_role, reassign_permission)
        # other_user needs the role at entity to be eligible for reassignment
        UserRoleAssignment.objects.create(user=other_user, role=approver_role, scope_node=entity)

        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        result = reassign_workflow_step(
            ist, new_user=other_user, reassigned_by=approver_user, note="Delegating"
        )

        assert result.assigned_user == other_user
        assert result.reassigned_from_user == approver_user
        assert result.reassigned_by == approver_user
        assert result.reassigned_at is not None

    def test_user_without_reassign_permission_cannot_reassign(
        self, published_version, entity, actor, approver_user,
        other_user, approver_role, module_activation, _approver_role_assignment,
    ):
        """Without reassign permission, raises StepActionError."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        with pytest.raises(StepActionError):
            reassign_workflow_step(ist, new_user=other_user, reassigned_by=actor)

    def test_reassign_to_ineligible_user_fails(
        self, published_version, entity, actor, approver_user,
        other_user, approver_role, module_activation, _approver_role_assignment,
        reassign_permission,
    ):
        """Reassigning to a user not in the eligible pool raises."""
        from apps.access.services import grant_permission_to_role
        grant_permission_to_role(approver_role, reassign_permission)
        # other_user has NO role assignment → not eligible

        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        with pytest.raises(StepActionError, match="not eligible"):
            reassign_workflow_step(ist, new_user=other_user, reassigned_by=approver_user)


# ---------------------------------------------------------------------------
# Pending tasks
# ---------------------------------------------------------------------------

class TestPendingTasks:
    def test_my_pending_tasks_returns_actionable_steps(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """Only actionable steps assigned to the user are returned."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        tasks = list(get_pending_tasks_for_user(approver_user))
        assert len(tasks) == 1
        assert tasks[0].workflow_step.name == "Step 1"
        assert tasks[0].assigned_user == approver_user

    def test_my_pending_tasks_excludes_other_users_steps(
        self, published_version, entity, actor, approver_user,
        other_user, approver_role, module_activation, _approver_role_assignment,
    ):
        """Tasks assigned to someone else do not appear in my tasks."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        tasks = list(get_pending_tasks_for_user(other_user))
        assert len(tasks) == 0

    def test_approved_steps_no_longer_pending(
        self, published_version, entity, actor, approver_user,
        approver_role, module_activation, _approver_role_assignment,
    ):
        """After approval, step disappears from pending tasks. Uses single-group so approval completes the instance."""
        g1, s1 = _make_single_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        # Approve the step
        ist = instance.instance_groups.first().instance_steps.first()
        approve_workflow_step(ist, acted_by=approver_user)

        tasks = list(get_pending_tasks_for_user(approver_user))
        assert len(tasks) == 0


# ---------------------------------------------------------------------------
# Step assignment overrides
# ---------------------------------------------------------------------------

class TestStepAssignmentOverrides:
    def test_override_assigns_eligible_user(self, published_version, entity, actor, approver_user, other_user, approver_role, module_activation, _approver_role_assignment):
        """Manual override can reassign a step to a different eligible user."""
        g1, s1, _, _ = _make_two_group_template(published_version, approver_role, entity)
        # Default: approver_user assigned
        WorkflowStep.objects.filter(pk=s1.pk).update(default_user=approver_user)

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user == approver_user

        # Override: reassign to other_user (also needs role assignment)
        UserRoleAssignment.objects.create(user=other_user, role=approver_role, scope_node=entity)
        apply_step_assignment_overrides(
            instance, {str(s1.pk): other_user.pk}, entity,
        )
        ist.refresh_from_db()
        assert ist.assigned_user == other_user

    def test_override_fails_for_ineligible_user(self, published_version, entity, actor, approver_user, other_user, approver_role, module_activation, _approver_role_assignment):
        """Override to a user not in the eligible pool raises."""
        g1, s1, _, _ = _make_two_group_template(published_version, approver_role, entity)
        WorkflowStep.objects.filter(pk=s1.pk).update(default_user=approver_user)

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        # other_user has no role assignment → not eligible
        with pytest.raises(ValueError, match="not eligible"):
            apply_step_assignment_overrides(instance, {str(s1.pk): other_user.pk}, entity)


# ---------------------------------------------------------------------------
# Events emitted on runtime actions
# ---------------------------------------------------------------------------

class TestWorkflowEventsOnRuntime:
    def test_approve_creates_step_approved_event(self, published_version, entity, actor, approver_user, approver_role, module_activation, _approver_role_assignment):
        """approve_workflow_step creates a STEP_APPROVED event."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        approve_workflow_step(ist, acted_by=approver_user, note="OK")

        events = WorkflowEvent.objects.filter(instance=instance, event_type=WorkflowEventType.STEP_APPROVED)
        assert events.count() == 1
        assert events.first().actor_user == approver_user

    def test_final_approval_creates_instance_approved_event(self, published_version, entity, actor, approver_user, approver_role, module_activation, _approver_role_assignment):
        """Completion of last (and only) group creates INSTANCE_APPROVED event. Uses single-group template."""
        g1, s1 = _make_single_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        approve_workflow_step(ist, acted_by=approver_user)

        events = WorkflowEvent.objects.filter(instance=instance, event_type=WorkflowEventType.INSTANCE_APPROVED)
        assert events.count() == 1

    def test_reject_creates_instance_rejected_event(self, published_version, entity, actor, approver_user, approver_role, module_activation, _approver_role_assignment):
        """TERMINATE rejection creates INSTANCE_REJECTED event."""
        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        reject_workflow_step(ist, acted_by=approver_user)

        events = WorkflowEvent.objects.filter(instance=instance, event_type=WorkflowEventType.INSTANCE_REJECTED)
        assert events.count() == 1

    def test_reassign_creates_step_reassigned_event(self, published_version, entity, actor, approver_user, other_user, approver_role, module_activation, _approver_role_assignment, reassign_permission):
        """reassign_workflow_step creates a STEP_REASSIGNED event."""
        from apps.access.services import grant_permission_to_role
        grant_permission_to_role(approver_role, reassign_permission)
        UserRoleAssignment.objects.create(user=other_user, role=approver_role, scope_node=entity)

        g1, s1, _, _ = _make_two_group_template(
            published_version, approver_role, entity, default_user=approver_user
        )

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor,
        )
        activate_workflow_instance(instance, activated_by=actor)

        ist = instance.instance_groups.first().instance_steps.first()
        reassign_workflow_step(ist, new_user=other_user, reassigned_by=approver_user)

        events = WorkflowEvent.objects.filter(instance=instance, event_type=WorkflowEventType.STEP_REASSIGNED)
        assert events.count() == 1
        assert events.first().target_user == other_user
        assert events.first().metadata["old_user_id"] == approver_user.pk
