import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.access.models import (
    Permission,
    PermissionAction,
    PermissionResource,
    Role,
    UserRoleAssignment,
)
from apps.access.services import grant_permission_to_role
from apps.audit.models import AuditLog
from apps.core.models import NodeType, Organization, ScopeNode
from apps.users.api.views.users import UserViewSet
from apps.users.models import User
from apps.workflow.models import (
    AssignmentState,
    BranchStatus,
    GroupStatus,
    InstanceStatus,
    ParallelMode,
    ScopeResolutionPolicy,
    StepGroup,
    StepKind,
    StepStatus,
    VersionStatus,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowInstance,
    WorkflowInstanceBranch,
    WorkflowInstanceGroup,
    WorkflowInstanceStep,
    WorkflowStep,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from apps.workflow.responsibility_services import (
    WorkflowResponsibilityError,
    bulk_reassign_workflow_responsibilities,
    get_workflow_responsibility_preview,
)
from apps.workflow.services import (
    StepActionError,
    reassign_workflow_branch,
    reassign_workflow_step,
)


@pytest.fixture
def responsibility_setup(db):
    org = Organization.objects.create(name="Responsibility Org", code="responsibility-org")
    node = ScopeNode.objects.create(
        org=org,
        name="Marketing",
        code="marketing",
        node_type=NodeType.DEPARTMENT,
        path="/responsibility-org/marketing",
        depth=0,
    )
    approver_role = Role.objects.create(
        org=org,
        name="Marketing Executive",
        code="marketing_executive",
        node_type_scope=NodeType.DEPARTMENT,
    )
    admin_role = Role.objects.create(
        org=org,
        name="Tenant Admin",
        code="tenant_admin",
        node_type_scope=NodeType.DEPARTMENT,
    )
    reassign_permission = Permission.objects.create(
        action=PermissionAction.REASSIGN,
        resource=PermissionResource.INVOICE,
    )
    grant_permission_to_role(admin_role, reassign_permission)

    actor = User.objects.create_user(
        email="admin@responsibility.test",
        password="pass",
        is_staff=True,
    )
    old_user = User.objects.create_user(email="old@responsibility.test", password="pass")
    new_user = User.objects.create_user(email="new@responsibility.test", password="pass")
    wrong_user = User.objects.create_user(email="wrong@responsibility.test", password="pass")
    UserRoleAssignment.objects.create(user=actor, role=admin_role, scope_node=node)
    UserRoleAssignment.objects.create(user=old_user, role=approver_role, scope_node=node)
    UserRoleAssignment.objects.create(user=new_user, role=approver_role, scope_node=node)

    template = WorkflowTemplate.objects.create(
        name="Invoice Workflow",
        code="invoice-workflow",
        module="invoice",
        scope_node=node,
        created_by=actor,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template,
        version_number=1,
        status=VersionStatus.PUBLISHED,
    )
    config_group = StepGroup.objects.create(
        template_version=version,
        name="Review",
        display_order=1,
        parallel_mode=ParallelMode.SINGLE,
    )
    config_step = WorkflowStep.objects.create(
        group=config_group,
        name="Marketing Review",
        required_role=approver_role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
        step_kind=StepKind.NORMAL_APPROVAL,
        default_user=old_user,
    )

    instance = WorkflowInstance.objects.create(
        template_version=version,
        subject_type="invoice",
        subject_id=1001,
        subject_scope_node=node,
        status=InstanceStatus.ACTIVE,
        started_by=actor,
    )
    runtime_group = WorkflowInstanceGroup.objects.create(
        instance=instance,
        step_group=config_group,
        display_order=1,
        status=GroupStatus.IN_PROGRESS,
    )
    runtime_step = WorkflowInstanceStep.objects.create(
        instance_group=runtime_group,
        workflow_step=config_step,
        assigned_user=old_user,
        assignment_state=AssignmentState.ASSIGNED,
        status=StepStatus.WAITING,
    )

    branch_instance = WorkflowInstance.objects.create(
        template_version=version,
        subject_type="invoice",
        subject_id=1002,
        subject_scope_node=node,
        status=InstanceStatus.ACTIVE,
        started_by=actor,
    )
    branch_group = WorkflowInstanceGroup.objects.create(
        instance=branch_instance,
        step_group=config_group,
        display_order=1,
        status=GroupStatus.IN_PROGRESS,
    )
    branch_parent = WorkflowInstanceStep.objects.create(
        instance_group=branch_group,
        workflow_step=config_step,
        assigned_user=actor,
        assignment_state=AssignmentState.ASSIGNED,
        status=StepStatus.WAITING_BRANCHES,
    )
    branch = WorkflowInstanceBranch.objects.create(
        parent_instance_step=branch_parent,
        instance=branch_instance,
        target_scope_node=node,
        assigned_user=old_user,
        assignment_state=AssignmentState.ASSIGNED,
        status=BranchStatus.PENDING,
    )

    return {
        "org": org,
        "node": node,
        "actor": actor,
        "old_user": old_user,
        "new_user": new_user,
        "wrong_user": wrong_user,
        "runtime_step": runtime_step,
        "branch": branch,
    }


def test_preview_returns_pending_step_branch_and_only_common_eligible_users(
    responsibility_setup,
):
    data = responsibility_setup
    preview = get_workflow_responsibility_preview(
        target_user=data["old_user"],
        actor=data["actor"],
    )
    assert preview["counts"] == {"steps": 1, "branches": 1, "total": 2}
    assert {item["task_kind"] for item in preview["responsibilities"]} == {
        "step",
        "branch",
    }
    candidate_ids = {user["id"] for user in preview["eligible_replacements"]}
    assert data["new_user"].pk in candidate_ids
    assert data["wrong_user"].pk not in candidate_ids


def test_bulk_reassignment_updates_all_pending_work_and_audits(responsibility_setup):
    data = responsibility_setup
    result = bulk_reassign_workflow_responsibilities(
        from_user=data["old_user"],
        to_user=data["new_user"],
        actor=data["actor"],
        reason="Kajal replacement",
    )
    assert result == {
        "steps_reassigned": 1,
        "branches_reassigned": 1,
        "total_reassigned": 2,
    }
    data["runtime_step"].refresh_from_db()
    data["branch"].refresh_from_db()
    assert data["runtime_step"].assigned_user == data["new_user"]
    assert data["branch"].assigned_user == data["new_user"]
    assert WorkflowEvent.objects.filter(
        event_type=WorkflowEventType.STEP_REASSIGNED
    ).count() == 1
    assert WorkflowEvent.objects.filter(
        event_type=WorkflowEventType.BRANCH_REASSIGNED
    ).count() == 1
    assert AuditLog.objects.filter(
        action="workflow_responsibilities_bulk_reassigned",
        resource_id=data["old_user"].pk,
    ).exists()


def test_bulk_reassignment_is_atomic_for_ineligible_replacement(responsibility_setup):
    data = responsibility_setup
    with pytest.raises(WorkflowResponsibilityError, match="not eligible"):
        bulk_reassign_workflow_responsibilities(
            from_user=data["old_user"],
            to_user=data["wrong_user"],
            actor=data["actor"],
            reason="Invalid replacement",
        )
    data["runtime_step"].refresh_from_db()
    data["branch"].refresh_from_db()
    assert data["runtime_step"].assigned_user == data["old_user"]
    assert data["branch"].assigned_user == data["old_user"]


def test_completed_step_and_branch_cannot_be_reassigned(responsibility_setup):
    data = responsibility_setup
    data["runtime_step"].status = StepStatus.APPROVED
    data["runtime_step"].save(update_fields=["status"])
    with pytest.raises(StepActionError, match="not WAITING"):
        reassign_workflow_step(
            data["runtime_step"], data["new_user"], data["actor"]
        )

    data["branch"].status = BranchStatus.APPROVED
    data["branch"].save(update_fields=["status"])
    with pytest.raises(StepActionError, match="not PENDING"):
        reassign_workflow_branch(
            data["branch"], data["new_user"], data["actor"]
        )


def test_user_deactivation_blocked_until_responsibilities_reassigned(
    responsibility_setup,
):
    data = responsibility_setup
    factory = APIRequestFactory()
    view = UserViewSet.as_view({"patch": "partial_update"})
    request = factory.patch(
        f"/users/{data['old_user'].pk}/",
        {"is_active": False},
        format="json",
    )
    force_authenticate(request, user=data["actor"])
    response = view(request, pk=data["old_user"].pk)
    assert response.status_code == 400
    assert "pending workflow" in str(response.data)

    bulk_reassign_workflow_responsibilities(
        from_user=data["old_user"],
        to_user=data["new_user"],
        actor=data["actor"],
        reason="Before deactivation",
    )
    second_request = factory.patch(
        f"/users/{data['old_user'].pk}/",
        {"is_active": False},
        format="json",
    )
    force_authenticate(second_request, user=data["actor"])
    second_response = view(second_request, pk=data["old_user"].pk)
    assert second_response.status_code == 200


def test_user_responsibility_preview_and_bulk_api(responsibility_setup):
    data = responsibility_setup
    factory = APIRequestFactory()

    preview_view = UserViewSet.as_view({"get": "workflow_responsibilities"})
    preview_request = factory.get(
        f"/users/{data['old_user'].pk}/workflow-responsibilities/"
    )
    force_authenticate(preview_request, user=data["actor"])
    preview_response = preview_view(preview_request, pk=data["old_user"].pk)
    assert preview_response.status_code == 200
    assert preview_response.data["counts"]["total"] == 2

    reassign_view = UserViewSet.as_view({
        "post": "reassign_workflow_responsibilities",
    })
    reassign_request = factory.post(
        f"/users/{data['old_user'].pk}/reassign-workflow-responsibilities/",
        {
            "new_user": data["new_user"].pk,
            "reason": "Replacement through People page",
        },
        format="json",
    )
    force_authenticate(reassign_request, user=data["actor"])
    reassign_response = reassign_view(reassign_request, pk=data["old_user"].pk)
    assert reassign_response.status_code == 200, reassign_response.data
    assert reassign_response.data["total_reassigned"] == 2
    assert reassign_response.data["remaining"]["total"] == 0
