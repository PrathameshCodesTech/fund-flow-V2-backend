import pytest
from apps.core.models import Organization, ScopeNode, NodeType
from apps.access.models import Role, UserRoleAssignment
from apps.users.models import User
from apps.workflow.models import (
    WorkflowTemplate,
    WorkflowTemplateVersion,
    StepGroup,
    WorkflowStep,
    WorkflowInstance,
    WorkflowEvent,
    WorkflowEventType,
    VersionStatus,
    InstanceStatus,
    GroupStatus,
    StepStatus,
    ScopeResolutionPolicy,
    ParallelMode,
    RejectionAction,
)
from apps.notifications.models import NotificationDelivery, NotificationChannel, NotificationStatus
from apps.workflow.models import AssignmentState, WorkflowInstanceStep
from apps.access.models import Permission, RolePermission, PermissionAction, PermissionResource
from apps.workflow.services import (
    create_workflow_instance_draft,
    activate_workflow_instance,
    apply_step_assignment_overrides,
    publish_template_version,
    resolve_step_target_node,
    reassign_workflow_step,
    StepActionError,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Org", code="wf-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/wf-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/wf-org/hq/ea", depth=1,
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="actor@example.com", password="pass")


@pytest.fixture
def approver(db):
    """A second user who will be the assigned approver."""
    return User.objects.create_user(email="approver@example.com", password="pass")


@pytest.fixture
def role(org):
    return Role.objects.create(org=org, name="Approver", code="approver")


@pytest.fixture
def template(entity, user):
    return WorkflowTemplate.objects.create(
        name="Invoice WF", module="invoice", scope_node=entity, created_by=user
    )


@pytest.fixture
def draft_version(template):
    return WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.DRAFT
    )


@pytest.fixture
def group(draft_version):
    return StepGroup.objects.create(
        template_version=draft_version,
        name="Approval Group",
        display_order=1,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )


@pytest.fixture
def step(group, role):
    """Step with no default_user — requires manual assignment before activation."""
    return WorkflowStep.objects.create(
        group=group,
        name="Manager Approval",
        required_role=role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
    )


@pytest.fixture
def step_with_default_user(group, role, approver, entity):
    """Step whose default_user holds the required role at entity."""
    UserRoleAssignment.objects.create(user=approver, role=role, scope_node=entity)
    step = WorkflowStep.objects.create(
        group=group,
        name="Manager Approval",
        required_role=role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        default_user=approver,
    )
    return step


def _assign_all_steps(instance, user):
    """Helper: forcibly assign all instance steps to a user so activation can proceed."""
    WorkflowInstance.objects.filter(pk=instance.pk)  # ensure loaded
    for ig in instance.instance_groups.all():
        ig.instance_steps.all().update(assigned_user=user)


# ---------------------------------------------------------------------------
# TestPublishTemplateVersion
# ---------------------------------------------------------------------------

class TestPublishTemplateVersion:
    def test_publish_draft(self, draft_version, user):
        version = publish_template_version(draft_version, published_by=user)
        assert version.status == VersionStatus.PUBLISHED
        assert version.published_by == user
        assert version.published_at is not None

    def test_cannot_publish_already_published(self, draft_version, user):
        publish_template_version(draft_version, published_by=user)
        draft_version.refresh_from_db()
        with pytest.raises(ValueError):
            publish_template_version(draft_version, published_by=user)

    def test_publishes_only_one_at_a_time(self, template, user):
        v1 = WorkflowTemplateVersion.objects.create(
            template=template, version_number=1, status=VersionStatus.DRAFT
        )
        v2 = WorkflowTemplateVersion.objects.create(
            template=template, version_number=2, status=VersionStatus.DRAFT
        )
        publish_template_version(v1, published_by=user)
        publish_template_version(v2, published_by=user)

        v1.refresh_from_db()
        v2.refresh_from_db()
        assert v1.status == VersionStatus.ARCHIVED
        assert v2.status == VersionStatus.PUBLISHED


# ---------------------------------------------------------------------------
# TestCreateWorkflowInstanceDraft  (Gap #3: WAITING not ORPHANED)
# ---------------------------------------------------------------------------

class TestCreateWorkflowInstanceDraft:
    def test_creates_instance_in_draft(self, draft_version, step, entity, user):
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=1,
            subject_scope_node=entity,
            started_by=user,
        )
        assert instance.status == InstanceStatus.DRAFT
        assert instance.subject_type == "invoice"
        assert instance.subject_id == 1

    def test_creates_instance_groups_and_steps(self, draft_version, group, step, entity, user):
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=2,
            subject_scope_node=entity,
            started_by=user,
        )
        assert instance.instance_groups.count() == 1
        ig = instance.instance_groups.first()
        assert ig.instance_steps.count() == 1

    def test_unassigned_step_is_waiting_not_orphaned(self, draft_version, group, step, entity, user):
        """
        Gap #3: steps without a valid default_user must be WAITING with assigned_user=None.
        ORPHANED is reserved for runtime invalidation, not draft creation.
        """
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=3,
            subject_scope_node=entity,
            started_by=user,
        )
        ig = instance.instance_groups.first()
        ist = ig.instance_steps.first()
        assert ist.status == StepStatus.WAITING
        assert ist.assigned_user is None

    def test_step_with_valid_default_user_gets_assigned(
        self, draft_version, group, step_with_default_user, entity, approver, user
    ):
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=4,
            subject_scope_node=entity,
            started_by=user,
        )
        ig = instance.instance_groups.first()
        ist = ig.instance_steps.first()
        assert ist.assigned_user == approver
        assert ist.status == StepStatus.WAITING


# ---------------------------------------------------------------------------
# TestActivationBlocksUnassigned  (Gap #2)
# ---------------------------------------------------------------------------

class TestActivationBlocksUnassigned:
    def test_activation_fails_when_step_unassigned(self, draft_version, group, step, entity, user):
        """Gap #2: DRAFT → ACTIVE must fail if any step has no assigned user."""
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=20,
            subject_scope_node=entity,
            started_by=user,
        )
        with pytest.raises(ValueError, match="no assigned user"):
            activate_workflow_instance(instance, activated_by=user)

    def test_activation_fails_when_one_of_many_steps_unassigned(
        self, draft_version, group, org, step_with_default_user, entity, role, approver, user
    ):
        """Even one unassigned step blocks activation."""
        # Add a second step using a role with NO holders → NO_ELIGIBLE_USERS
        empty_role = Role.objects.create(org=org, name="Empty Role", code="empty-role")
        WorkflowStep.objects.create(
            group=group,
            name="Second Approval",
            required_role=empty_role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=2,
        )
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=21,
            subject_scope_node=entity,
            started_by=user,
        )
        # First step is assigned (has default_user), second has no eligible users
        with pytest.raises(ValueError, match="no assigned user"):
            activate_workflow_instance(instance, activated_by=user)

    def test_activation_succeeds_when_all_steps_assigned(
        self, draft_version, group, step, entity, user, approver
    ):
        """Gap #2: activation must proceed once all steps have assigned users."""
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=22,
            subject_scope_node=entity,
            started_by=user,
        )
        _assign_all_steps(instance, approver)
        activated = activate_workflow_instance(instance, activated_by=user)
        assert activated.status == InstanceStatus.ACTIVE
        assert activated.current_group is not None
        assert activated.started_at is not None

    def test_first_group_only_transitions_after_validation(
        self, draft_version, group, step, entity, user, approver
    ):
        """First group must stay WAITING until activation validation passes."""
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=23,
            subject_scope_node=entity,
            started_by=user,
        )
        first_group = instance.instance_groups.order_by("display_order").first()
        assert first_group.status == GroupStatus.WAITING

        # Attempt fails — group must stay WAITING (transaction rolled back)
        with pytest.raises(ValueError):
            activate_workflow_instance(instance, activated_by=user)

        first_group.refresh_from_db()
        assert first_group.status == GroupStatus.WAITING

        # Now assign and retry
        _assign_all_steps(instance, approver)
        activate_workflow_instance(instance, activated_by=user)
        first_group.refresh_from_db()
        assert first_group.status == GroupStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# TestActivateWorkflowInstance (existing lifecycle tests, updated)
# ---------------------------------------------------------------------------

class TestActivateWorkflowInstance:
    def test_cannot_activate_active_instance(
        self, draft_version, group, step, entity, user, approver
    ):
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=11,
            subject_scope_node=entity,
            started_by=user,
        )
        _assign_all_steps(instance, approver)
        activate_workflow_instance(instance, activated_by=user)
        instance.refresh_from_db()
        with pytest.raises(ValueError, match="current status is 'ACTIVE'"):
            activate_workflow_instance(instance, activated_by=user)


# ---------------------------------------------------------------------------
# TestWorkflowEvents  (Gap #4)
# ---------------------------------------------------------------------------

class TestWorkflowEvents:
    def test_activation_creates_step_assigned_events(
        self, draft_version, group, step, entity, user, approver
    ):
        """Gap #4: STEP_ASSIGNED event emitted for each assigned step in first group."""
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=30,
            subject_scope_node=entity,
            started_by=user,
        )
        _assign_all_steps(instance, approver)
        activate_workflow_instance(instance, activated_by=user)

        events = WorkflowEvent.objects.filter(
            instance=instance, event_type=WorkflowEventType.STEP_ASSIGNED
        )
        assert events.count() == 1
        event = events.first()
        assert event.actor_user == user
        assert event.target_user == approver

    def test_activation_creates_in_app_notification_deliveries(
        self, draft_version, group, step, entity, user, approver
    ):
        """Gap #4: in_app NotificationDelivery rows created with pending status."""
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=31,
            subject_scope_node=entity,
            started_by=user,
        )
        _assign_all_steps(instance, approver)
        activate_workflow_instance(instance, activated_by=user)

        deliveries = NotificationDelivery.objects.filter(
            event__instance=instance,
            channel=NotificationChannel.IN_APP,
        )
        assert deliveries.count() == 1
        assert deliveries.first().status == NotificationStatus.PENDING

    def test_events_target_the_assigned_users_of_first_group(
        self, draft_version, group, role, entity, user, approver
    ):
        """Gap #4: events are for assigned users of the first active group only."""
        user2 = User.objects.create_user(email="approver2@example.com", password="pass")
        step1 = WorkflowStep.objects.create(
            group=group,
            name="Step 1",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        step2 = WorkflowStep.objects.create(
            group=group,
            name="Step 2",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=2,
        )

        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=32,
            subject_scope_node=entity,
            started_by=user,
        )
        # Assign each step to a different user
        steps = list(instance.instance_groups.first().instance_steps.order_by("workflow_step__display_order"))
        steps[0].assigned_user = approver
        steps[0].save()
        steps[1].assigned_user = user2
        steps[1].save()

        activate_workflow_instance(instance, activated_by=user)

        events = WorkflowEvent.objects.filter(
            instance=instance, event_type=WorkflowEventType.STEP_ASSIGNED
        )
        target_users = set(events.values_list("target_user_id", flat=True))
        assert target_users == {approver.pk, user2.pk}

        # Two deliveries — one per step
        deliveries = NotificationDelivery.objects.filter(event__instance=instance)
        assert deliveries.count() == 2

    def test_no_events_when_draft_created_without_activation(
        self, draft_version, group, step, entity, user
    ):
        """Draft creation itself emits no events."""
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=33,
            subject_scope_node=entity,
            started_by=user,
        )
        assert WorkflowEvent.objects.filter(instance=instance).count() == 0


# ---------------------------------------------------------------------------
# TestResolveStepTargetNode
# ---------------------------------------------------------------------------

class TestResolveStepTargetNode:
    def test_subject_node_policy(self, step, entity):
        result = resolve_step_target_node(step, entity)
        assert result.pk == entity.pk

    def test_ancestor_of_type_policy(self, group, role, entity, company):
        ancestor_step = WorkflowStep.objects.create(
            group=group,
            name="Company Approval",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.ANCESTOR_OF_TYPE,
            ancestor_node_type=NodeType.COMPANY,
            display_order=2,
        )
        result = resolve_step_target_node(ancestor_step, entity)
        assert result.pk == company.pk

    def test_org_root_policy(self, group, role, entity, company):
        root_step = WorkflowStep.objects.create(
            group=group,
            name="Root Approval",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.ORG_ROOT,
            display_order=3,
        )
        result = resolve_step_target_node(root_step, entity)
        assert result.pk == company.pk

    def test_fixed_node_policy(self, group, role, company, entity):
        fixed_step = WorkflowStep.objects.create(
            group=group,
            name="Fixed Approval",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.FIXED_NODE,
            fixed_scope_node=company,
            display_order=4,
        )
        result = resolve_step_target_node(fixed_step, entity)
        assert result.pk == company.pk


# ---------------------------------------------------------------------------
# TestAssignmentState  (Gap A: assignment policy)
# ---------------------------------------------------------------------------

class TestAssignmentState:
    def test_auto_assign_when_exactly_one_eligible(self, draft_version, group, role, entity, approver, user):
        """Single eligible user with no default_user → ASSIGNED automatically."""
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=entity)
        WorkflowStep.objects.create(
            group=group,
            name="Auto Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=50,
            subject_scope_node=entity,
            started_by=user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user == approver
        assert ist.assignment_state == AssignmentState.ASSIGNED

    def test_assignment_required_when_multiple_eligible_no_default(
        self, draft_version, group, role, entity, approver, user
    ):
        """Multiple eligible users, no default_user → ASSIGNMENT_REQUIRED, assigned_user=None."""
        user2 = User.objects.create_user(email="approver2@example.com", password="pass")
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=entity)
        UserRoleAssignment.objects.create(user=user2, role=role, scope_node=entity)
        WorkflowStep.objects.create(
            group=group,
            name="Multi Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=51,
            subject_scope_node=entity,
            started_by=user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user is None
        assert ist.assignment_state == AssignmentState.ASSIGNMENT_REQUIRED

    def test_no_eligible_users_state(self, draft_version, group, role, entity, user):
        """Zero eligible users → NO_ELIGIBLE_USERS, assigned_user=None."""
        WorkflowStep.objects.create(
            group=group,
            name="Orphan Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=52,
            subject_scope_node=entity,
            started_by=user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user is None
        assert ist.assignment_state == AssignmentState.NO_ELIGIBLE_USERS

    def test_inactive_default_user_excluded_falls_through(
        self, draft_version, group, role, entity, approver, user
    ):
        """Inactive default_user is excluded → falls through to single-eligible auto-assign."""
        approver.is_active = False
        approver.save()
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=entity)
        active_user = User.objects.create_user(email="active@example.com", password="pass")
        UserRoleAssignment.objects.create(user=active_user, role=role, scope_node=entity)
        WorkflowStep.objects.create(
            group=group,
            name="Inactive Default Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
            default_user=approver,
        )
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=53,
            subject_scope_node=entity,
            started_by=user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user == active_user
        assert ist.assignment_state == AssignmentState.ASSIGNED

    def test_editing_step_default_user_does_not_mutate_existing_instance(
        self, draft_version, group, step_with_default_user, entity, approver, user
    ):
        """Version immutability: changing the template step's default_user after instance creation
        must not affect the already-created instance's assigned_user."""
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=54,
            subject_scope_node=entity,
            started_by=user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        original_assigned = ist.assigned_user

        # Mutate the template step's default_user
        step_with_default_user.default_user = None
        step_with_default_user.save()

        ist.refresh_from_db()
        assert ist.assigned_user == original_assigned

    def test_reassignment_updates_only_running_instance(
        self, draft_version, group, role, entity, approver, user
    ):
        """Reassigning a step on one instance does not affect any other instance."""
        # Grant user the REASSIGN:INVOICE permission via role assignment
        perm, _ = Permission.objects.get_or_create(
            action=PermissionAction.REASSIGN, resource=PermissionResource.INVOICE
        )
        RolePermission.objects.create(role=role, permission=perm)
        UserRoleAssignment.objects.create(user=user, role=role, scope_node=entity)
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=entity)

        WorkflowStep.objects.create(
            group=group,
            name="Reassign Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        instance1 = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=55,
            subject_scope_node=entity,
            started_by=user,
        )
        instance2 = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=56,
            subject_scope_node=entity,
            started_by=user,
        )
        # Assign instance1 steps and activate
        _assign_all_steps(instance1, approver)
        activate_workflow_instance(instance1, activated_by=user)

        new_user = User.objects.create_user(email="new@example.com", password="pass")
        UserRoleAssignment.objects.create(user=new_user, role=role, scope_node=entity)

        ist1 = instance1.instance_groups.first().instance_steps.first()
        reassign_workflow_step(ist1, new_user=new_user, reassigned_by=user)

        # instance2's step must remain unchanged
        ist2 = instance2.instance_groups.first().instance_steps.first()
        ist2.refresh_from_db()
        assert ist2.assigned_user != new_user

    def test_future_instances_use_new_version(self, template, entity, user, role):
        """Future instances use the newly published version, not the old one."""
        v1 = WorkflowTemplateVersion.objects.create(
            template=template, version_number=1, status=VersionStatus.DRAFT
        )
        g1 = StepGroup.objects.create(
            template_version=v1, name="Group A", display_order=1,
            parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=g1, name="V1 Step", required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE, display_order=1,
        )
        publish_template_version(v1, published_by=user)

        # Create v2 and publish
        v2 = WorkflowTemplateVersion.objects.create(
            template=template, version_number=2, status=VersionStatus.DRAFT
        )
        g2 = StepGroup.objects.create(
            template_version=v2, name="Group B", display_order=1,
            parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=g2, name="V2 Step", required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE, display_order=1,
        )
        publish_template_version(v2, published_by=user)

        instance = create_workflow_instance_draft(
            template_version=v2,
            subject_type="invoice",
            subject_id=57,
            subject_scope_node=entity,
            started_by=user,
        )
        assert instance.template_version_id == v2.pk
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.workflow_step.name == "V2 Step"


# ---------------------------------------------------------------------------
# TestApplyStepAssignmentOverrides  (Part B)
# ---------------------------------------------------------------------------

class TestApplyStepAssignmentOverrides:
    def test_override_sets_assigned_user_and_assignment_state(
        self, draft_version, group, role, entity, approver, user
    ):
        """
        apply_step_assignment_overrides must set both assigned_user and
        assignment_state=ASSIGNED, not leave a stale ASSIGNMENT_REQUIRED.
        """
        # Two eligible users → step starts as ASSIGNMENT_REQUIRED, assigned_user=None
        user2 = User.objects.create_user(email="approver2b@example.com", password="pass")
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=entity)
        UserRoleAssignment.objects.create(user=user2, role=role, scope_node=entity)
        step = WorkflowStep.objects.create(
            group=group,
            name="Override Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=60,
            subject_scope_node=entity,
            started_by=user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user is None
        assert ist.assignment_state == AssignmentState.ASSIGNMENT_REQUIRED

        apply_step_assignment_overrides(
            instance,
            {str(step.pk): approver.pk},
            entity,
        )

        ist.refresh_from_db()
        assert ist.assigned_user == approver
        assert ist.assignment_state == AssignmentState.ASSIGNED

    def test_override_recovers_from_no_eligible_users(
        self, draft_version, group, org, role, entity, approver, user
    ):
        """
        An admin can assign a user to a NO_ELIGIBLE_USERS step after adding
        the required role assignment, transitioning it to ASSIGNED.
        """
        empty_role = Role.objects.create(org=org, name="Empty Role 2", code="empty-role-2")
        step = WorkflowStep.objects.create(
            group=group,
            name="No Eligible Step",
            required_role=empty_role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=61,
            subject_scope_node=entity,
            started_by=user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assignment_state == AssignmentState.NO_ELIGIBLE_USERS

        # Now add the role assignment — user becomes eligible
        UserRoleAssignment.objects.create(user=approver, role=empty_role, scope_node=entity)

        apply_step_assignment_overrides(
            instance,
            {str(step.pk): approver.pk},
            entity,
        )

        ist.refresh_from_db()
        assert ist.assigned_user == approver
        assert ist.assignment_state == AssignmentState.ASSIGNED
