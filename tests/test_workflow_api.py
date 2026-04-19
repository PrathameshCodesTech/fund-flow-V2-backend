"""
API-level tests for workflow endpoints.
Verifies that action endpoints enforce correct authorization at the API layer.
"""
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate
from apps.core.models import Organization, ScopeNode, NodeType
from apps.access.models import Role, Permission, PermissionAction, PermissionResource, UserRoleAssignment
from apps.access.services import grant_permission_to_role, assign_user_role
from apps.users.models import User
from apps.invoices.models import Invoice, InvoiceStatus
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    VersionStatus, StepStatus, GroupStatus, InstanceStatus,
    ParallelMode, RejectionAction, ScopeResolutionPolicy,
)
from apps.workflow.api.views.instances import (
    WorkflowInstanceStepViewSet,
    WorkflowInstanceViewSet,
    MyTasksView,
)
from apps.workflow.services import create_workflow_instance_draft, activate_workflow_instance
from apps.modules.models import ModuleActivation, ModuleType


@pytest.fixture
def factory():
    return APIRequestFactory()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="WF API Org", code="wfa-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/wfa-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/wfa-org/hq/ea", depth=1,
    )


@pytest.fixture
def invoice_owner(db):
    """Separate user — the creator of the invoice (no workflow permissions needed to create)."""
    return User.objects.create_user(email="invoice-owner@example.com", password="pass")


@pytest.fixture
def actor_user(db):
    """User who will attempt workflow actions — not the invoice creator."""
    return User.objects.create_user(email="actor@example.com", password="pass")


@pytest.fixture
def approver_user(db):
    return User.objects.create_user(email="approver@example.com", password="pass")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="pass")


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
def approver_role(org):
    return Role.objects.create(org=org, name="Approver", code="approver")


@pytest.fixture
def start_wf_role(org, start_wf_permission):
    role = Role.objects.create(org=org, name="Workflow Starter", code="wf_starter")
    grant_permission_to_role(role, start_wf_permission)
    return role


@pytest.fixture
def reassign_role(org, reassign_permission):
    role = Role.objects.create(org=org, name="Reassigner", code="reassigner")
    grant_permission_to_role(role, reassign_permission)
    return role


@pytest.fixture
def module_activation(entity):
    return ModuleActivation.objects.create(
        module=ModuleType.INVOICE, scope_node=entity,
        is_active=True, override_parent=True,
    )


@pytest.fixture
def template(entity, invoice_owner):
    return WorkflowTemplate.objects.create(
        name="Invoice WF", module="invoice", scope_node=entity, created_by=invoice_owner
    )


@pytest.fixture
def published_version(template):
    return WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.PUBLISHED
    )


@pytest.fixture
def single_group(published_version, approver_role):
    group = StepGroup.objects.create(
        template_version=published_version,
        name="Group 1",
        display_order=1,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    WorkflowStep.objects.create(
        group=group, name="Step 1", required_role=approver_role,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        display_order=1,
    )
    return group


@pytest.fixture
def invoice(entity, invoice_owner):
    return Invoice.objects.create(
        title="Test Invoice", amount="1000.00", currency="INR",
        scope_node=entity, created_by=invoice_owner, status=InvoiceStatus.DRAFT,
    )


def _make_request(factory, method, path, user, data=None):
    fn = getattr(factory, method)
    request = fn(path, data, format="json") if data else fn(path)
    force_authenticate(request, user=user)
    return request


# ---------------------------------------------------------------------------
# Tests: from-invoice endpoint
# ---------------------------------------------------------------------------

class TestFromInvoiceEndpoint:
    def test_from_invoice_denied_without_permission(
        self, factory, invoice, actor_user, entity, start_wf_role, module_activation, single_group,
    ):
        """
        Actor is not the creator and has no START_WORKFLOW permission → 403.
        """
        # Actor has no role at any node. Invoice creator is invoice_owner (separate).
        assert invoice.created_by != actor_user

        request = _make_request(
            factory, "post", "/instances/from-invoice/",
            actor_user, {"invoice_id": invoice.pk}
        )
        view = WorkflowInstanceViewSet.as_view({"post": "from_invoice"})
        response = view(request)
        assert response.status_code == 403

    def test_from_invoice_succeeds_for_creator(
        self, factory, invoice, invoice_owner, module_activation, single_group,
    ):
        """Invoice creator can start a workflow without any role assignment."""
        request = _make_request(
            factory, "post", "/instances/from-invoice/",
            invoice_owner, {"invoice_id": invoice.pk}
        )
        view = WorkflowInstanceViewSet.as_view({"post": "from_invoice"})
        response = view(request)
        assert response.status_code == 201, f"got {response.status_code}: {response.data}"
        assert response.data["status"] == InstanceStatus.DRAFT

    def test_from_invoice_succeeds_for_start_wf_permission(
        self, factory, invoice, actor_user, entity, start_wf_role, module_activation, single_group,
    ):
        """User with START_WORKFLOW permission at invoice scope can start workflow."""
        # Actor is not the creator
        assert invoice.created_by != actor_user
        # But has permission
        assign_user_role(actor_user, start_wf_role, entity)

        request = _make_request(
            factory, "post", "/instances/from-invoice/",
            actor_user, {"invoice_id": invoice.pk}
        )
        view = WorkflowInstanceViewSet.as_view({"post": "from_invoice"})
        response = view(request)
        assert response.status_code == 201, f"got {response.status_code}: {response.data}"

    def test_from_invoice_returns_404_for_missing_invoice(
        self, factory, invoice_owner, module_activation, single_group,
    ):
        """Non-existent invoice returns 404."""
        request = _make_request(
            factory, "post", "/instances/from-invoice/",
            invoice_owner, {"invoice_id": 99999}
        )
        view = WorkflowInstanceViewSet.as_view({"post": "from_invoice"})
        response = view(request)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: step approve/reject/reassign actions
# ---------------------------------------------------------------------------

class TestApproveAction:
    def test_approve_returns_403_for_wrong_user(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, single_group, published_version,
    ):
        """Only the assigned user can approve; wrong user gets 403."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        # Give actor_user visible scope so they can reach the step, but not be the assigned user.
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        step = single_group.steps.first()
        step.default_user = approver_user
        step.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        activate_workflow_instance(instance, activated_by=actor_user)
        ist = instance.instance_groups.first().instance_steps.first()

        # actor_user tries to approve — should be denied
        request = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/approve/",
            actor_user, {"note": "trying to approve"}
        )
        view = WorkflowInstanceStepViewSet.as_view({"post": "approve"})
        response = view(request, pk=ist.pk)
        assert response.status_code == 403

    def test_approve_succeeds_for_assigned_user(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, single_group, published_version,
    ):
        """Assigned user can approve their step."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        step = single_group.steps.first()
        step.default_user = approver_user
        step.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        activate_workflow_instance(instance, activated_by=actor_user)
        ist = instance.instance_groups.first().instance_steps.first()

        request = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/approve/",
            approver_user, {"note": "LGTM"}
        )
        view = WorkflowInstanceStepViewSet.as_view({"post": "approve"})
        response = view(request, pk=ist.pk)
        assert response.status_code == 200
        assert response.data["status"] == StepStatus.APPROVED


class TestRejectAction:
    def test_reject_returns_403_for_wrong_user(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, single_group, published_version,
    ):
        """Only the assigned user can reject."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        step = single_group.steps.first()
        step.default_user = approver_user
        step.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        activate_workflow_instance(instance, activated_by=actor_user)
        ist = instance.instance_groups.first().instance_steps.first()

        request = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/reject/",
            actor_user, {"note": "trying to reject"}
        )
        view = WorkflowInstanceStepViewSet.as_view({"post": "reject"})
        response = view(request, pk=ist.pk)
        assert response.status_code == 403


class TestReassignAction:
    def test_reassign_returns_403_without_permission(
        self, factory, entity, actor_user, approver_user, other_user, approver_role,
        module_activation, single_group, published_version,
    ):
        """Reassign requires REASSIGN permission; actor_user has none."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        UserRoleAssignment.objects.create(user=other_user, role=approver_role, scope_node=entity)
        # Give actor_user visible scope so they can reach the step, but no REASSIGN permission.
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        step = single_group.steps.first()
        step.default_user = approver_user
        step.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        activate_workflow_instance(instance, activated_by=actor_user)
        ist = instance.instance_groups.first().instance_steps.first()

        # actor_user tries to reassign — denied (no reassign permission)
        request = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/reassign/",
            actor_user, {"user_id": other_user.pk}
        )
        view = WorkflowInstanceStepViewSet.as_view({"post": "reassign"})
        response = view(request, pk=ist.pk)
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tests: tasks/me endpoint
# ---------------------------------------------------------------------------

class TestMyTasksEndpoint:
    def test_tasks_me_returns_assigned_user_tasks(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, single_group, published_version,
    ):
        """GET /tasks/me/ returns steps assigned to the requesting user."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        step = single_group.steps.first()
        step.default_user = approver_user
        step.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        activate_workflow_instance(instance, activated_by=actor_user)
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user == approver_user

        request = _make_request(factory, "get", "/tasks/me/", approver_user)
        view = MyTasksView.as_view()
        response = view(request)
        assert response.status_code == 200
        step_ids = [t["instance_step_id"] for t in response.data]
        assert ist.pk in step_ids

    def test_tasks_me_does_not_return_other_users_tasks(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, single_group, published_version,
    ):
        """GET /tasks/me/ does NOT return steps assigned to other users."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        step = single_group.steps.first()
        step.default_user = approver_user
        step.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        activate_workflow_instance(instance, activated_by=actor_user)
        # actor_user has no assigned steps
        request = _make_request(factory, "get", "/tasks/me/", actor_user)
        view = MyTasksView.as_view()
        response = view(request)
        assert response.status_code == 200
        assert len(response.data) == 0


# ---------------------------------------------------------------------------
# Tests: assignment-plan and draft assign
# ---------------------------------------------------------------------------

class TestAssignmentPlan:
    def test_assignment_plan_returns_groups_and_steps_in_order(
        self, factory, entity, actor_user, approver_role, module_activation, published_version, single_group,
    ):
        """GET /instances/{id}/assignment-plan/ returns groups and steps in display_order."""
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )

        request = _make_request(factory, "get", f"/instances/{instance.pk}/assignment-plan/", actor_user)
        view = WorkflowInstanceViewSet.as_view({"get": "assignment_plan"})
        response = view(request, pk=instance.pk)
        assert response.status_code == 200

        plan = response.data
        assert plan["instance_id"] == instance.pk
        assert plan["status"] == InstanceStatus.DRAFT
        assert plan["subject_type"] == "invoice"
        assert len(plan["groups"]) == 1
        assert plan["groups"][0]["name"] == "Group 1"
        assert plan["groups"][0]["display_order"] == 1
        assert len(plan["groups"][0]["steps"]) == 1
        assert plan["groups"][0]["steps"][0]["step_name"] == "Step 1"

    def test_assignment_plan_eligible_users_are_role_and_scope_based(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, published_version, single_group,
    ):
        """Eligible users list contains only users with required role at resolved node."""
        # approver_user has the role at entity
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )

        request = _make_request(factory, "get", f"/instances/{instance.pk}/assignment-plan/", actor_user)
        view = WorkflowInstanceViewSet.as_view({"get": "assignment_plan"})
        response = view(request, pk=instance.pk)
        assert response.status_code == 200

        step = response.data["groups"][0]["steps"][0]
        eligible_ids = [u["id"] for u in step["eligible_users"]]
        assert approver_user.pk in eligible_ids

    def test_assignment_plan_shows_null_assigned_user_when_unassigned(
        self, factory, entity, actor_user, approver_role, module_activation, published_version, single_group,
    ):
        """When no default_user is set, assigned_user is null in plan."""
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        # Ensure no default assignment happened
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user is None

        request = _make_request(factory, "get", f"/instances/{instance.pk}/assignment-plan/", actor_user)
        view = WorkflowInstanceViewSet.as_view({"get": "assignment_plan"})
        response = view(request, pk=instance.pk)
        assert response.status_code == 200
        assert response.data["groups"][0]["steps"][0]["assigned_user"] is None


class TestDraftAssign:
    def test_assign_accepts_eligible_user(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, published_version, single_group,
    ):
        """POST /instance-steps/{id}/assign/ with eligible user succeeds."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        # actor_user needs actionable scope at entity to call the assign action.
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user is None

        request = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/assign/",
            actor_user, {"user_id": approver_user.pk}
        )
        view = WorkflowInstanceStepViewSet.as_view({"post": "assign"})
        response = view(request, pk=ist.pk)
        assert response.status_code == 200
        assert response.data["assigned_user"] == approver_user.pk

    def test_assign_rejects_ineligible_user(
        self, factory, entity, actor_user, other_user, approver_role,
        module_activation, published_version, single_group,
    ):
        """POST /instance-steps/{id}/assign/ with ineligible user returns 400."""
        # actor_user needs actionable scope; other_user does NOT have the required role.
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        ist = instance.instance_groups.first().instance_steps.first()

        request = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/assign/",
            actor_user, {"user_id": other_user.pk}
        )
        view = WorkflowInstanceStepViewSet.as_view({"post": "assign"})
        response = view(request, pk=ist.pk)
        assert response.status_code == 400
        assert "not eligible" in response.data["detail"]

    def test_assign_rejects_assignment_on_active_instance(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, published_version, single_group,
    ):
        """POST /instance-steps/{id}/assign/ on ACTIVE instance returns 400."""
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)
        # actor_user needs actionable scope to pass the authority check before the DRAFT check.
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        step = single_group.steps.first()
        step.default_user = approver_user
        step.save()

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        activate_workflow_instance(instance, activated_by=actor_user)
        ist = instance.instance_groups.first().instance_steps.first()

        request = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/assign/",
            actor_user, {"user_id": approver_user.pk}
        )
        view = WorkflowInstanceStepViewSet.as_view({"post": "assign"})
        response = view(request, pk=ist.pk)
        assert response.status_code == 400
        assert "DRAFT" in response.data["detail"]

    def test_activate_succeeds_after_all_steps_assigned(
        self, factory, entity, actor_user, approver_user, approver_role,
        module_activation, published_version, single_group,
    ):
        """Instance activates successfully when all steps have assigned users."""
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        UserRoleAssignment.objects.create(user=approver_user, role=approver_role, scope_node=entity)

        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        ist = instance.instance_groups.first().instance_steps.first()

        # Assign the step
        assign_req = _make_request(
            factory, "post", f"/instance-steps/{ist.pk}/assign/",
            actor_user, {"user_id": approver_user.pk}
        )
        assign_view = WorkflowInstanceStepViewSet.as_view({"post": "assign"})
        assign_resp = assign_view(assign_req, pk=ist.pk)
        assert assign_resp.status_code == 200

        # Activate
        act_req = _make_request(factory, "post", f"/instances/{instance.pk}/activate/", actor_user)
        act_view = WorkflowInstanceViewSet.as_view({"post": "activate"})
        act_resp = act_view(act_req, pk=instance.pk)
        assert act_resp.status_code == 200
        assert act_resp.data["status"] == InstanceStatus.ACTIVE

    def test_activate_fails_when_step_unassigned(
        self, factory, entity, actor_user, approver_role, module_activation, published_version, single_group,
    ):
        """Instance activation fails when any step has no assigned user."""
        UserRoleAssignment.objects.create(user=actor_user, role=approver_role, scope_node=entity)
        instance = create_workflow_instance_draft(
            template_version=published_version,
            subject_type="invoice", subject_id=1,
            subject_scope_node=entity, started_by=actor_user,
        )
        # Leave the step unassigned (no default_user was set)
        ist = instance.instance_groups.first().instance_steps.first()
        assert ist.assigned_user is None

        request = _make_request(factory, "post", f"/instances/{instance.pk}/activate/", actor_user)
        view = WorkflowInstanceViewSet.as_view({"post": "activate"})
        response = view(request, pk=instance.pk)
        assert response.status_code == 400
        assert "no assigned user" in response.data["detail"]
