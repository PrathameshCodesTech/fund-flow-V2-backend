"""
Tests for:
  GET  /api/v1/invoices/pending-review/
  POST /api/v1/invoices/{id}/begin-review/

Authorization model (permission-based):
  User can begin review if:
    1. user_has_permission_including_ancestors(user, START_WORKFLOW, INVOICE, scope)
       OR
    2. User is eligible for the first actionable human step of the selected route.

Roles are configurable bundles; permissions are the stable authorization contract.
"""
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.access.models import (
    Role, Permission, PermissionAction, PermissionResource,
    RolePermission,
)
from apps.access.services import grant_permission_to_role, assign_user_role
from apps.invoices.models import Invoice, InvoiceStatus
from apps.invoices.api.views import InvoiceViewSet
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    WorkflowInstance, WorkflowInstanceStep,
    VersionStatus, InstanceStatus, ScopeResolutionPolicy, StepKind,
    ParallelMode, RejectionAction, BranchApprovalPolicy, AllocationTotalPolicy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="PR Org", code="pr-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/pr-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/pr-org/hq/ea", depth=1,
    )


@pytest.fixture
def actor(db):
    return User.objects.create_user(email="actor@pr.com", password="pass")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@pr.com", password="pass")


@pytest.fixture
def approver_role(org):
    return Role.objects.create(org=org, name="Approver", code="approver")


@pytest.fixture
def start_workflow_permission(db):
    perm, _ = Permission.objects.get_or_create(
        action=PermissionAction.START_WORKFLOW,
        resource=PermissionResource.INVOICE,
    )
    return perm


@pytest.fixture
def approve_permission(db):
    perm, _ = Permission.objects.get_or_create(
        action=PermissionAction.APPROVE,
        resource=PermissionResource.INVOICE,
    )
    return perm


@pytest.fixture
def org_admin_role(org, start_workflow_permission):
    """Role with START_WORKFLOW:INVOICE — represents any privileged role."""
    role = Role.objects.create(org=org, name="Org Admin", code="org_admin")
    grant_permission_to_role(role, start_workflow_permission)
    return role


@pytest.fixture
def marketing_head_role(org):
    """Role with no START_WORKFLOW — should only access via first-step eligibility."""
    return Role.objects.create(org=org, name="Marketing Head", code="marketing_head")


@pytest.fixture
def pending_invoice(org, entity, actor):
    return Invoice.objects.create(
        title="PR Test Invoice", amount="5000.00", currency="INR",
        scope_node=entity, created_by=actor, status=InvoiceStatus.PENDING_WORKFLOW,
    )


def _make_published_template(node, user, code="default", version_number=1):
    """Create an active WorkflowTemplate with one published version and one human step."""
    template = WorkflowTemplate.objects.create(
        name=f"WF {code}", code=code, module="invoice",
        scope_node=node, is_active=True, created_by=user,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template, version_number=version_number, status=VersionStatus.PUBLISHED,
    )
    group = StepGroup.objects.create(
        template_version=version, name="Stage 1", display_order=0,
        parallel_mode=ParallelMode.SINGLE,
        on_rejection_action=RejectionAction.TERMINATE,
    )
    return template, version, group


def _add_step(group, role, scope_policy=ScopeResolutionPolicy.SUBJECT_NODE, order=0, kind=StepKind.NORMAL_APPROVAL):
    return WorkflowStep.objects.create(
        group=group, name="Step 1", required_role=role,
        scope_resolution_policy=scope_policy, display_order=order,
        step_kind=kind,
    )


def _pending_review_get(factory, user):
    request = factory.get("/invoices/pending-review/")
    force_authenticate(request, user=user)
    view = InvoiceViewSet.as_view({"get": "pending_review"})
    return view(request)


def _begin_review_post(factory, user, invoice_pk, data):
    request = factory.post("/invoices/begin-review/", data, format="json")
    force_authenticate(request, user=user)
    view = InvoiceViewSet.as_view({"post": "begin_review"})
    return view(request, pk=invoice_pk)


# ---------------------------------------------------------------------------
# pending-review: visibility
# ---------------------------------------------------------------------------

class TestPendingReviewVisibility:
    def test_first_step_eligible_user_sees_invoice(self, factory, pending_invoice, entity, actor, approver_role):
        """User holding the first-step role at entity sees the invoice via first-step eligibility."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        ids = [r["id"] for r in response.data]
        assert pending_invoice.id in ids

    def test_start_workflow_permission_sees_invoice(self, factory, pending_invoice, entity, actor, org_admin_role, approver_role):
        """User with START_WORKFLOW:INVOICE permission sees the invoice via permission gate."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        # Assign org_admin (has START_WORKFLOW:INVOICE) at ancestor level
        assign_user_role(actor, org_admin_role, entity)

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        ids = [r["id"] for r in response.data]
        assert pending_invoice.id in ids

    def test_approve_only_permission_cannot_see_invoice(
        self, factory, pending_invoice, entity, actor, approver_role, approve_permission, org
    ):
        """
        User with APPROVE:INVOICE but not first-step eligible and no START_WORKFLOW
        is correctly excluded from pending-review.
        """
        approve_role = Role.objects.create(org=org, name="Approver Only", code="approve-only")
        grant_permission_to_role(approve_role, approve_permission)
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        # actor has approver_role but is not assigned to it (not first-step eligible)
        # approve_role gives APPROVE but not START_WORKFLOW → excluded
        assign_user_role(actor, approve_role, entity)

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        ids = [r["id"] for r in response.data]
        assert pending_invoice.id not in ids

    def test_marketing_head_without_permission_or_first_step_excluded(
        self, factory, pending_invoice, entity, actor, marketing_head_role, approver_role
    ):
        """
        marketing_head without START_WORKFLOW and not first-step eligible is excluded.
        If the product wants marketing_head to claim, grant it START_WORKFLOW:INVOICE.
        """
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, marketing_head_role, entity)

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        ids = [r["id"] for r in response.data]
        assert pending_invoice.id not in ids

    def test_non_eligible_user_excluded(self, factory, pending_invoice, entity, actor, other_user, approver_role):
        """User with no role assignment sees no invoices in queue."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _pending_review_get(factory, other_user)
        assert response.status_code == 200
        ids = [r["id"] for r in response.data]
        assert pending_invoice.id not in ids

    def test_excludes_invoice_already_attached(self, factory, pending_invoice, entity, actor, approver_role):
        """Invoice with selected_workflow_version is excluded from queue."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)
        pending_invoice.selected_workflow_version = version
        pending_invoice.save(update_fields=["selected_workflow_version"])

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        ids = [r["id"] for r in response.data]
        assert pending_invoice.id not in ids

    def test_excludes_invoice_with_active_instance(self, factory, pending_invoice, entity, actor, approver_role):
        """Invoice already having a non-rejected workflow instance is excluded."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        WorkflowInstance.objects.create(
            template_version=version,
            subject_type="invoice", subject_id=pending_invoice.pk,
            subject_scope_node=entity, status=InstanceStatus.ACTIVE,
            started_by=actor,
        )

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        ids = [r["id"] for r in response.data]
        assert pending_invoice.id not in ids

    def test_available_routes_includes_first_step_name(self, factory, pending_invoice, entity, actor, approver_role):
        """Queue result includes first_step_name and user_can_begin for each route."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        invoice_row = next(r for r in response.data if r["id"] == pending_invoice.id)
        routes = invoice_row["available_routes"]
        assert len(routes) == 1
        assert routes[0]["first_step_name"] == "Step 1"
        assert routes[0]["user_can_begin"] is True


# ---------------------------------------------------------------------------
# begin-review: activation path
# ---------------------------------------------------------------------------

class TestBeginReviewActivation:
    def test_begin_review_activates_when_all_steps_assigned(self, factory, pending_invoice, entity, actor, approver_role):
        """begin-review returns status=activated when all steps have default/auto assignees."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 200
        assert response.data["status"] == "activated"
        assert response.data["invoice_id"] == pending_invoice.id
        assert "workflow_instance_id" in response.data

        pending_invoice.refresh_from_db()
        assert pending_invoice.status == InvoiceStatus.IN_REVIEW

    def test_begin_review_returns_assignment_required_when_later_steps_unassigned(
        self, factory, pending_invoice, entity, actor, approver_role, org
    ):
        """begin-review returns assignment_required when a later step has no eligible user."""
        other_role = Role.objects.create(org=org, name="Other Role", code="other-role-br")
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role, order=0)
        WorkflowStep.objects.create(
            group=group, name="Step 2", required_role=other_role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE, display_order=1,
            step_kind=StepKind.NORMAL_APPROVAL,
        )
        assign_user_role(actor, approver_role, entity)
        # No one assigned to other_role → step2 unassigned

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 200
        assert response.data["status"] == "assignment_required"
        assert "workflow_instance_id" in response.data

        instance = WorkflowInstance.objects.get(pk=response.data["workflow_instance_id"])
        assert instance.status == InstanceStatus.DRAFT

    def test_begin_review_assigns_actor_to_first_step(self, factory, pending_invoice, entity, actor, approver_role):
        """Actor eligible for first step is auto-assigned to that step."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 200

        instance_id = response.data["workflow_instance_id"]
        ist = WorkflowInstanceStep.objects.filter(
            instance_group__instance_id=instance_id
        ).first()
        assert ist.assigned_user_id == actor.id

    def test_begin_review_with_start_workflow_permission_activates(
        self, factory, pending_invoice, entity, actor, start_workflow_permission, org
    ):
        """
        begin-review succeeds via START_WORKFLOW permission alone
        (no first-step role assignment needed).
        """
        sw_role = Role.objects.create(
            org=org, name="Workflow Starter", code="wf-starter"
        )
        grant_permission_to_role(sw_role, start_workflow_permission)
        assign_user_role(actor, sw_role, entity)

        _, version, group = _make_published_template(entity, actor)
        # Template with no steps that would auto-assign actor
        group2 = StepGroup.objects.create(
            template_version=version, name="Stage 2", display_order=1,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        # Step with no default_user so it won't auto-assign actor
        WorkflowStep.objects.create(
            group=group, name="Step 1", required_role=sw_role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE, display_order=0,
            step_kind=StepKind.NORMAL_APPROVAL,
            default_user=None,
        )

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 200
        assert response.data["status"] in ("activated", "assignment_required")

    def test_begin_review_ancestor_scope_workflow_succeeds(
        self, factory, pending_invoice, entity, company, actor, approver_role
    ):
        """Workflow configured at ancestor (company) is valid for entity invoice."""
        _, version, group = _make_published_template(company, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 200
        assert response.data["status"] in ("activated", "assignment_required")


# ---------------------------------------------------------------------------
# begin-review: second claim / race protection
# ---------------------------------------------------------------------------

class TestBeginReviewRaceProtection:
    def test_begin_review_rejects_second_claim(
        self, factory, pending_invoice, entity, actor, approver_role
    ):
        """Second begin-review call on same invoice returns 400."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        pending_invoice.refresh_from_db()
        response2 = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response2.status_code == 400


# ---------------------------------------------------------------------------
# begin-review: input validation
# ---------------------------------------------------------------------------

class TestBeginReviewValidation:
    def test_begin_review_rejects_draft_invoice(self, factory, entity, actor, approver_role):
        """begin-review on a DRAFT invoice returns 400."""
        draft = Invoice.objects.create(
            title="Draft", amount="100.00", currency="INR",
            scope_node=entity, created_by=actor, status=InvoiceStatus.DRAFT,
        )
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _begin_review_post(factory, actor, draft.pk, {"template_version_id": version.id})
        assert response.status_code == 400

    def test_begin_review_rejects_inactive_template(self, factory, pending_invoice, entity, actor, approver_role):
        """Inactive template is rejected."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        version.template.is_active = False
        version.template.save(update_fields=["is_active"])

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 400
        assert "not active" in response.data["detail"]

    def test_begin_review_rejects_non_published_version(self, factory, pending_invoice, entity, actor, approver_role):
        """Draft version cannot be used for begin-review."""
        template = WorkflowTemplate.objects.create(
            name="Draft Template", code="draft-t", module="invoice",
            scope_node=entity, is_active=True, created_by=actor,
        )
        draft_version = WorkflowTemplateVersion.objects.create(
            template=template, version_number=1, status=VersionStatus.DRAFT,
        )
        assign_user_role(actor, approver_role, entity)

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": draft_version.id})
        assert response.status_code == 400
        assert "published" in response.data["detail"]

    def test_begin_review_rejects_wrong_module(self, factory, pending_invoice, entity, actor, approver_role, org):
        """Campaign workflow version cannot be used for an invoice."""
        campaign_template = WorkflowTemplate.objects.create(
            name="Campaign WF", code="campaign-wf", module="campaign",
            scope_node=entity, is_active=True, created_by=actor,
        )
        campaign_version = WorkflowTemplateVersion.objects.create(
            template=campaign_template, version_number=1, status=VersionStatus.PUBLISHED,
        )
        assign_user_role(actor, approver_role, entity)

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": campaign_version.id})
        assert response.status_code == 400
        assert "invoice module" in response.data["detail"]

    def test_begin_review_rejects_out_of_scope_template(
        self, factory, pending_invoice, entity, company, actor, approver_role, org
    ):
        """Template at a sibling node is rejected."""
        sibling = ScopeNode.objects.create(
            org=org, parent=company, name="Entity B", code="eb",
            node_type=NodeType.ENTITY, path="/pr-org/hq/eb", depth=1,
        )
        _, version, group = _make_published_template(sibling, actor, code="sibling-wf")
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 400
        assert "scope" in response.data["detail"]

    def test_begin_review_rejects_user_without_permission_or_first_step(
        self, factory, pending_invoice, entity, actor, other_user, approver_role
    ):
        """User with no START_WORKFLOW and not first-step eligible gets 403."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)
        # other_user has no role at all

        response = _begin_review_post(factory, other_user, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 403

    def test_approve_only_role_cannot_begin_review(
        self, factory, pending_invoice, entity, actor, approver_role, approve_permission, org
    ):
        """
        User with APPROVE:INVOICE but not first-step eligible and no START_WORKFLOW
        gets 403 — APPROVE is for acting on assigned steps, not claiming routes.
        """
        approve_role = Role.objects.create(org=org, name="Approver Only", code="approve-only")
        grant_permission_to_role(approve_role, approve_permission)
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approve_role, entity)

        response = _begin_review_post(factory, actor, pending_invoice.pk, {"template_version_id": version.id})
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# pending-review: route visibility (START_WORKFLOW vs first-step-only)
# ---------------------------------------------------------------------------

class TestPendingReviewRouteVisibility:
    def test_start_workflow_user_sees_all_routes(
        self, factory, pending_invoice, entity, actor, org_admin_role, org
    ):
        """
        User with START_WORKFLOW:INVOICE sees ALL active published routes,
        not just routes they are first-step eligible for.
        """
        # Two templates at entity: one actor is first-step for, one they are not
        _, ver1, group1 = _make_published_template(entity, actor, code="route-a")
        _add_step(group1, org_admin_role)   # actor CAN be first step (org_admin_role)
        assign_user_role(actor, org_admin_role, entity)

        other_role = Role.objects.create(org=org, name="Other Approver", code="other-appr")
        _, ver2, group2 = _make_published_template(entity, actor, code="route-b")
        _add_step(group2, other_role)        # actor CANNOT be first step here

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        invoice_row = next(r for r in response.data if r["id"] == pending_invoice.id)
        route_codes = {r["template_code"] for r in invoice_row["available_routes"]}
        assert "route-a" in route_codes
        assert "route-b" in route_codes  # visible via START_WORKFLOW even though not first-step

    def test_first_step_only_user_sees_only_actionable_routes(
        self, factory, pending_invoice, entity, actor, approver_role, org
    ):
        """
        User without START_WORKFLOW sees ONLY routes they are first-step eligible for.
        Routes they cannot start are not shown.
        """
        # Route A: actor is first-step eligible
        _, ver_a, group_a = _make_published_template(entity, actor, code="can-begin")
        _add_step(group_a, approver_role)
        assign_user_role(actor, approver_role, entity)

        # Route B: actor is NOT first-step eligible
        other_role = Role.objects.create(org=org, name="Other Approver", code="other-appr-2")
        _, ver_b, group_b = _make_published_template(entity, actor, code="cannot-begin")
        _add_step(group_b, other_role)

        response = _pending_review_get(factory, actor)
        assert response.status_code == 200
        invoice_row = next(r for r in response.data if r["id"] == pending_invoice.id)
        route_codes = {r["template_code"] for r in invoice_row["available_routes"]}
        assert "can-begin" in route_codes
        assert "cannot-begin" not in route_codes  # hidden — actor cannot begin this route

    def test_approve_only_user_sees_no_routes(
        self, factory, pending_invoice, entity, actor, approver_role, approve_permission, org
    ):
        """
        User with only APPROVE:INVOICE (no START_WORKFLOW, not first-step eligible)
        sees no routes and the invoice row is excluded.
        """
        _, ver, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        approve_role = Role.objects.create(org=org, name="Appr Only", code="appr-only")
        grant_permission_to_role(approve_role, approve_permission)
        other_user = User.objects.create_user(email="appr-only@pr.com", password="pass")
        assign_user_role(other_user, approve_role, entity)

        response = _pending_review_get(factory, other_user)
        assert response.status_code == 200
        invoice_ids = [r["id"] for r in response.data]
        assert pending_invoice.id not in invoice_ids


# ---------------------------------------------------------------------------
# attach-workflow: hardening
# ---------------------------------------------------------------------------

class TestAttachWorkflowHardening:
    def test_attach_workflow_rejects_second_attach(
        self, factory, pending_invoice, entity, actor, approver_role
    ):
        """Second attach-workflow call on same invoice returns 400."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        req = factory.post(
            "/invoices/attach-workflow/",
            {"template_version_id": version.id},
            format="json",
        )
        force_authenticate(req, user=actor)
        view = InvoiceViewSet.as_view({"post": "attach_workflow"})

        r1 = view(req, pk=pending_invoice.pk)
        assert r1.status_code == 201

        pending_invoice.refresh_from_db()
        r2 = view(req, pk=pending_invoice.pk)
        assert r2.status_code == 400

    def test_attach_workflow_rejects_locked_invoice(self, factory, pending_invoice, entity, actor, approver_role):
        """
        Simulate a locked invoice by holding select_for_update before the API call.
        The endpoint should return 409 when it cannot acquire the lock.
        PostgreSQL-only: SQLite does not enforce NOWAIT lock semantics.
        """
        from django.db import connection
        if connection.vendor != "postgresql":
            pytest.skip("select_for_update(nowait=True) lock contention requires PostgreSQL")

        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        # Lock the invoice row before making the API request
        from apps.invoices.models import Invoice as InvoiceModel
        InvoiceModel.objects.select_for_update(nowait=True).get(pk=pending_invoice.pk)

        req = factory.post(
            "/invoices/attach-workflow/",
            {"template_version_id": version.id},
            format="json",
        )
        force_authenticate(req, user=actor)
        view = InvoiceViewSet.as_view({"post": "attach_workflow"})
        r = view(req, pk=pending_invoice.pk)

        assert r.status_code == 409
        assert "being processed" in r.data["detail"]

    def test_attach_workflow_rejects_invoice_with_active_instance(
        self, factory, pending_invoice, entity, actor, approver_role
    ):
        """attach-workflow returns 400 when a non-rejected instance already exists."""
        _, version, group = _make_published_template(entity, actor)
        _add_step(group, approver_role)
        assign_user_role(actor, approver_role, entity)

        # Pre-existing active instance
        WorkflowInstance.objects.create(
            template_version=version,
            subject_type="invoice",
            subject_id=pending_invoice.pk,
            subject_scope_node=entity,
            status=InstanceStatus.ACTIVE,
            started_by=actor,
        )

        req = factory.post(
            "/invoices/attach-workflow/",
            {"template_version_id": version.id},
            format="json",
        )
        force_authenticate(req, user=actor)
        view = InvoiceViewSet.as_view({"post": "attach_workflow"})
        r = view(req, pk=pending_invoice.pk)
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# ME template scope: ancestor discovery and eligibility
# ---------------------------------------------------------------------------

class TestMeTemplateScopeDiscovery:
    """
    Tests for the Phase 2 correction: ME workflow templates must live at the
    marketing department node (depth=0, ancestor of all regional nodes), NOT at
    corporate (sibling, not an ancestor).

    Route discovery walks [invoice.scope_node + ancestors]; only ancestor templates
    are visible to regional invoices.
    """

    def test_me_template_at_marketing_scope_eligible_for_regional_invoice(
        self, factory, pending_invoice, entity, actor, approver_role, org, db,
    ):
        """
        A template at the marketing department node is discoverable for a
        north-region invoice because marketing is an ancestor of north.
        """
        from apps.invoices.selectors import get_invoice_eligible_workflow_routes

        # marketing department node (depth=0, parent=None)
        marketing = ScopeNode.objects.create(
            org=org, parent=None, name="Marketing", code="marketing",
            node_type=NodeType.DEPARTMENT, path="/pr-org/marketing", depth=0,
        )
        # north region node (depth=1, child of marketing)
        north = ScopeNode.objects.create(
            org=org, parent=marketing, name="North", code="north",
            node_type=NodeType.REGION, path="/pr-org/marketing/north", depth=1,
        )

        # Override invoice to be at north
        pending_invoice.scope_node = north
        pending_invoice.save(update_fields=["scope_node"])

        # ME template at marketing node (ancestor of north)
        me_template = WorkflowTemplate.objects.create(
            name="Invoice 3-Step ME1", code="invoice-3-step-me1",
            module="invoice", scope_node=marketing, is_active=True, created_by=actor,
        )
        me_version = WorkflowTemplateVersion.objects.create(
            template=me_template, version_number=1, status=VersionStatus.PUBLISHED,
        )
        me_group = StepGroup.objects.create(
            template_version=me_version, name="ME Allocation", display_order=1,
            parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.RETURN_TO_SUBMITTER,
        )
        WorkflowStep.objects.create(
            group=me_group, name="ME Allocation", display_order=1,
            required_role=approver_role,
            scope_resolution_policy=ScopeResolutionPolicy.FIXED_NODE,
            fixed_scope_node=marketing,
            step_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
            branch_approval_policy=BranchApprovalPolicy.SKIP_ALL,
            allocation_total_policy=AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
            require_budget=True, require_category=True, require_subcategory=True,
            allow_multiple_lines_per_entity=True,
        )

        routes = get_invoice_eligible_workflow_routes(pending_invoice, user=actor)
        route_codes = [r["template_code"] for r in routes]

        assert "invoice-3-step-me1" in route_codes, (
            f"ME template at marketing should be eligible for north invoice. "
            f"Got routes: {route_codes}"
        )

    def test_begin_review_accepts_me_template_for_regional_invoice(
        self, factory, pending_invoice, entity, actor, approver_role, org, db,
    ):
        """
        A regional invoice (scope_node=north) can attach and activate a workflow
        whose template lives at the marketing ancestor node.
        """
        # Build hierarchy: marketing -> north
        marketing = ScopeNode.objects.create(
            org=org, parent=None, name="Marketing", code="marketing",
            node_type=NodeType.DEPARTMENT, path="/pr-org/marketing", depth=0,
        )
        north = ScopeNode.objects.create(
            org=org, parent=marketing, name="North", code="north",
            node_type=NodeType.REGION, path="/pr-org/marketing/north", depth=1,
        )

        pending_invoice.scope_node = north
        pending_invoice.save(update_fields=["scope_node"])

        # ME template at marketing node
        me_template = WorkflowTemplate.objects.create(
            name="Invoice 3-Step ME1", code="invoice-3-step-me1",
            module="invoice", scope_node=marketing, is_active=True, created_by=actor,
        )
        me_version = WorkflowTemplateVersion.objects.create(
            template=me_template, version_number=1, status=VersionStatus.PUBLISHED,
        )
        me_group = StepGroup.objects.create(
            template_version=me_version, name="ME Allocation", display_order=1,
            parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.RETURN_TO_SUBMITTER,
        )
        WorkflowStep.objects.create(
            group=me_group, name="ME Allocation", display_order=1,
            required_role=approver_role,
            scope_resolution_policy=ScopeResolutionPolicy.FIXED_NODE,
            fixed_scope_node=marketing,
            step_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
            branch_approval_policy=BranchApprovalPolicy.SKIP_ALL,
            allocation_total_policy=AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
            require_budget=True, require_category=True, require_subcategory=True,
            allow_multiple_lines_per_entity=True,
        )

        # Give actor START_WORKFLOW at north node
        start_perm = Permission.objects.get_or_create(
            action=PermissionAction.START_WORKFLOW, resource=PermissionResource.INVOICE,
        )[0]
        grant_permission_to_role(approver_role, start_perm)
        assign_user_role(actor, approver_role, north)

        request = factory.post(
            "/invoices/begin-review/", {"template_version_id": me_version.id}, format="json",
        )
        force_authenticate(request, user=actor)
        view = InvoiceViewSet.as_view({"post": "begin_review"})
        response = view(request, pk=pending_invoice.pk)

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data}"
        )
        assert response.data["status"] in ("activated", "assignment_required")

    def test_begin_review_rejects_corporate_template_for_regional_invoice(
        self, factory, pending_invoice, entity, actor, approver_role, org, db,
    ):
        """
        A template at corporate (sibling of north, NOT an ancestor) must be
        rejected for a north invoice — this was the original defect.
        """
        # Build hierarchy: marketing -> [corporate, north] (siblings)
        marketing = ScopeNode.objects.create(
            org=org, parent=None, name="Marketing", code="marketing",
            node_type=NodeType.DEPARTMENT, path="/pr-org/marketing", depth=0,
        )
        corporate = ScopeNode.objects.create(
            org=org, parent=marketing, name="Corporate", code="corporate",
            node_type=NodeType.REGION, path="/pr-org/marketing/corporate", depth=1,
        )
        north = ScopeNode.objects.create(
            org=org, parent=marketing, name="North", code="north",
            node_type=NodeType.REGION, path="/pr-org/marketing/north", depth=1,
        )

        pending_invoice.scope_node = north
        pending_invoice.save(update_fields=["scope_node"])

        # Template at corporate (sibling — not an ancestor of north)
        corp_template = WorkflowTemplate.objects.create(
            name="Invoice 3-Step ME1", code="invoice-3-step-me1",
            module="invoice", scope_node=corporate, is_active=True, created_by=actor,
        )
        corp_version = WorkflowTemplateVersion.objects.create(
            template=corp_template, version_number=1, status=VersionStatus.PUBLISHED,
        )
        corp_group = StepGroup.objects.create(
            template_version=corp_version, name="ME Allocation", display_order=1,
            parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.RETURN_TO_SUBMITTER,
        )
        from apps.workflow.models import BranchApprovalPolicy, AllocationTotalPolicy
        WorkflowStep.objects.create(
            group=corp_group, name="ME Allocation", display_order=1,
            required_role=approver_role,
            scope_resolution_policy=ScopeResolutionPolicy.FIXED_NODE,
            fixed_scope_node=corporate,
            step_kind=StepKind.RUNTIME_SPLIT_ALLOCATION,
            branch_approval_policy=BranchApprovalPolicy.SKIP_ALL,
            allocation_total_policy=AllocationTotalPolicy.MUST_EQUAL_INVOICE_TOTAL,
            require_budget=True, require_category=True, require_subcategory=True,
            allow_multiple_lines_per_entity=True,
        )

        start_perm = Permission.objects.get_or_create(
            action=PermissionAction.START_WORKFLOW, resource=PermissionResource.INVOICE,
        )[0]
        grant_permission_to_role(approver_role, start_perm)
        assign_user_role(actor, approver_role, north)

        request = factory.post(
            "/invoices/begin-review/", {"template_version_id": corp_version.id}, format="json",
        )
        force_authenticate(request, user=actor)
        view = InvoiceViewSet.as_view({"post": "begin_review"})
        response = view(request, pk=pending_invoice.pk)

        # Corporate template should NOT be eligible for north invoice
        assert response.status_code == 400, (
            f"Corporate template should be rejected for north invoice. "
            f"Got {response.status_code}: {response.data}"
        )
