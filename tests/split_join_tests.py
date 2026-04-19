# ---------------------------------------------------------------------------
# Split/Join Tests
# ---------------------------------------------------------------------------

from apps.workflow.models import (
    StepKind, BranchStatus, JoinPolicy,
    WorkflowInstanceBranch,
)


class TestSplitInstanceStep:
    """Tests for the split activation path."""

    @pytest.fixture
    def draft_version_with_split(self, template, user, entity, role):
        """Version with one group containing a SPLIT_BY_SCOPE step."""
        version = WorkflowTemplateVersion.objects.create(
            template=template,
            version_number=1,
            status=VersionStatus.DRAFT,
        )
        group = StepGroup.objects.create(
            template_version=version,
            name="Split Group",
            display_order=0,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=group,
            name="Branch Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=0,
            step_kind=StepKind.SPLIT_BY_SCOPE,
            split_target_mode="CHILD_NODES",
        )
        return version

    def test_split_creates_branches_for_each_child_node(
        self, draft_version_with_split, entity, company, role, approver, user
    ):
        """SPLIT_BY_SCOPE step creates one branch per child scope node."""
        child1 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 1", code="cu1",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu1", depth=2,
        )
        child2 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 2", code="cu2",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu2", depth=2,
        )
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child1)
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child2)

        instance = create_workflow_instance_draft(
            template_version=draft_version_with_split,
            subject_type="invoice",
            subject_id=100,
            subject_scope_node=entity,
            started_by=user,
        )
        activate_workflow_instance(instance, activated_by=user)

        split_step = instance.instance_groups.first().instance_steps.first()
        from apps.workflow.services import split_instance_step
        branches = split_instance_step(split_step)

        assert len(branches) == 2
        assert all(b.status == BranchStatus.PENDING for b in branches)
        assert all(b.target_scope_node_id in [child1.id, child2.id] for b in branches)
        split_step.refresh_from_db()
        assert split_step.status == StepStatus.WAITING_BRANCHES

    def test_split_is_idempotent(
        self, draft_version_with_split, entity, company, role, approver, user
    ):
        """Calling split twice returns the same branches, no duplicates."""
        child1 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 1", code="cu1",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu1", depth=2,
        )
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child1)

        instance = create_workflow_instance_draft(
            template_version=draft_version_with_split,
            subject_type="invoice",
            subject_id=101,
            subject_scope_node=entity,
            started_by=user,
        )
        activate_workflow_instance(instance, activated_by=user)

        split_step = instance.instance_groups.first().instance_steps.first()
        from apps.workflow.services import split_instance_step
        branches1 = split_instance_step(split_step)
        branches2 = split_instance_step(split_step)

        assert len(branches1) == len(branches2)
        assert set(b.id for b in branches1) == set(b.id for b in branches2)

    def test_split_step_is_not_actionable_directly(
        self, draft_version_with_split, entity, company, role, approver, user
    ):
        """A SPLIT_BY_SCOPE step cannot be approved/rejected directly."""
        child1 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 1", code="cu1",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu1", depth=2,
        )
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child1)

        instance = create_workflow_instance_draft(
            template_version=draft_version_with_split,
            subject_type="invoice",
            subject_id=102,
            subject_scope_node=entity,
            started_by=user,
        )
        activate_workflow_instance(instance, activated_by=user)

        split_step = instance.instance_groups.first().instance_steps.first()

        from apps.workflow.services import approve_workflow_step, StepActionError
        with pytest.raises(StepActionError):
            approve_workflow_step(split_step, acted_by=approver, note="")


class TestApproveBranch:
    """Tests for branch approval and join logic."""

    @pytest.fixture
    def draft_version_with_split_and_join(self, template, user, entity, role):
        """Version: Group1 (split) -> Group2 (join) -> Group3 (normal)."""
        version = WorkflowTemplateVersion.objects.create(
            template=template,
            version_number=1,
            status=VersionStatus.DRAFT,
        )
        group1 = StepGroup.objects.create(
            template_version=version,
            name="Split Group",
            display_order=0,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=group1,
            name="Branch Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=0,
            step_kind=StepKind.SPLIT_BY_SCOPE,
            split_target_mode="CHILD_NODES",
        )
        group2 = StepGroup.objects.create(
            template_version=version,
            name="Join Group",
            display_order=1,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=group2,
            name="Join Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=0,
            step_kind=StepKind.JOIN_BRANCHES,
            join_policy=JoinPolicy.ALL_BRANCHES_MUST_COMPLETE,
        )
        group3 = StepGroup.objects.create(
            template_version=version,
            name="Final Group",
            display_order=2,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=group3,
            name="Final Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=0,
        )
        return version

    def test_approving_all_branches_advances_to_join_step(
        self, draft_version_with_split_and_join, entity, role, approver, user
    ):
        """All branch approvals trigger join and advance to next group."""
        child1 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 1", code="cu1",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu1", depth=2,
        )
        child2 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 2", code="cu2",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu2", depth=2,
        )
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child1)
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child2)

        instance = create_workflow_instance_draft(
            template_version=draft_version_with_split_and_join,
            subject_type="invoice",
            subject_id=200,
            subject_scope_node=entity,
            started_by=user,
        )
        activate_workflow_instance(instance, activated_by=user)

        split_step = instance.instance_groups.filter(
            step_group__name="Split Group"
        ).first().instance_steps.first()

        from apps.workflow.services import split_instance_step
        branches = split_instance_step(split_step)

        from apps.workflow.services import approve_workflow_branch
        for branch in branches:
            approve_workflow_branch(branch, acted_by=approver, note="approved")

        g1 = instance.instance_groups.filter(step_group__name="Split Group").first()
        g1.refresh_from_db()
        assert g1.status == GroupStatus.APPROVED

        instance.refresh_from_db()
        assert instance.status == InstanceStatus.ACTIVE
        assert instance.current_group.step_group.name == "Join Group"

    def test_branch_rejection_terminates_instance(
        self, draft_version_with_split_and_join, entity, role, approver, user
    ):
        """Any branch rejection follows rejection policy and terminates."""
        child1 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 1", code="cu1",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu1", depth=2,
        )
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child1)

        instance = create_workflow_instance_draft(
            template_version=draft_version_with_split_and_join,
            subject_type="invoice",
            subject_id=201,
            subject_scope_node=entity,
            started_by=user,
        )
        activate_workflow_instance(instance, activated_by=user)

        split_step = instance.instance_groups.filter(
            step_group__name="Split Group"
        ).first().instance_steps.first()

        from apps.workflow.services import split_instance_step
        branches = split_instance_step(split_step)

        from apps.workflow.services import reject_workflow_branch
        reject_workflow_branch(branches[0], acted_by=approver, note="rejecting")

        instance.refresh_from_db()
        assert instance.status == InstanceStatus.REJECTED


class TestBranchReassign:
    """Tests for branch reassignment."""

    @pytest.fixture
    def draft_version_split(self, template, user, entity, role):
        version = WorkflowTemplateVersion.objects.create(
            template=template,
            version_number=1,
            status=VersionStatus.DRAFT,
        )
        group = StepGroup.objects.create(
            template_version=version,
            name="Split Group",
            display_order=0,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=group,
            name="Branch Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=0,
            step_kind=StepKind.SPLIT_BY_SCOPE,
            split_target_mode="CHILD_NODES",
        )
        return version

    def test_reassign_branch_to_another_eligible_user(
        self, draft_version_split, entity, role, approver, user
    ):
        """A branch can be reassigned to another user eligible at that scope."""
        child1 = ScopeNode.objects.create(
            org=entity.org, parent=entity, name="Child Unit 1", code="cu1",
            node_type=NodeType.ENTITY, path="/wf-org/hq/ea/cu1", depth=2,
        )
        approver2 = User.objects.create_user(email="approver2@example.com", password="pass")
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=child1)
        UserRoleAssignment.objects.create(user=approver2, role=role, scope_node=child1)

        instance = create_workflow_instance_draft(
            template_version=draft_version_split,
            subject_type="invoice",
            subject_id=300,
            subject_scope_node=entity,
            started_by=user,
        )
        activate_workflow_instance(instance, activated_by=user)

        split_step = instance.instance_groups.first().instance_steps.first()
        from apps.workflow.services import split_instance_step
        branches = split_instance_step(split_step)

        from apps.workflow.services import reassign_workflow_branch
        reassign_workflow_branch(
            branches[0],
            new_user=approver2,
            reassigned_by=user,
            note="reassigning",
        )

        branches[0].refresh_from_db()
        assert branches[0].assigned_user == approver2


class TestNormalWorkflowStillWorks:
    """Verify non-split workflows are unaffected by split/join changes."""

    def test_normal_approval_still_advances_group(
        self, draft_version, group, role, entity, approver, user
    ):
        """A normal step can be approved and the group advances."""
        step = WorkflowStep.objects.create(
            group=group,
            name="Normal Step",
            required_role=role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
            step_kind=StepKind.NORMAL_APPROVAL,
        )
        UserRoleAssignment.objects.create(user=approver, role=role, scope_node=entity)

        instance = create_workflow_instance_draft(
            template_version=draft_version,
            subject_type="invoice",
            subject_id=400,
            subject_scope_node=entity,
            started_by=user,
        )
        activate_workflow_instance(instance, activated_by=user)

        ist = instance.instance_groups.first().instance_steps.filter(
            workflow_step=step
        ).first()
        from apps.workflow.services import approve_workflow_step
        approve_workflow_step(ist, acted_by=approver, note="ok")

        group.refresh_from_db()
        assert group.status == GroupStatus.APPROVED