"""
Tests for the VendorSubmissionRoute (Send-To) feature.

Coverage:
  A. Route config model
  B. Vendor invoice submission requires send_to_option_id
  C. Auto-routing creates invoice + workflow instance + activates
  D. Misconfiguration blocking (no published version / unresolved assignees)
  E. Scope correctness (vendor cannot use route outside allowed org)
"""
import pytest
from decimal import Decimal
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.access.models import Role, Permission, PermissionAction, PermissionResource
from apps.vendors.models import (
    Vendor, UserVendorAssignment, OperationalStatus,
    VendorSubmissionRoute,
)
from apps.invoices.models import (
    VendorInvoiceSubmission, VendorInvoiceSubmissionStatus, InvoiceStatus,
)
from apps.invoices.services import (
    submit_vendor_invoice_with_route,
    SubmissionStateError,
    VendorRouteError,
)
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    WorkflowInstance, VersionStatus, InstanceStatus,
    ScopeResolutionPolicy, StepKind, ParallelMode, RejectionAction,
)
from apps.workflow.services import publish_template_version
from apps.invoices.api.views import VendorInvoiceSubmissionViewSet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_submission(vendor, scope_node, user, status=VendorInvoiceSubmissionStatus.READY):
    return VendorInvoiceSubmission.objects.create(
        vendor=vendor,
        submitted_by=user,
        scope_node=scope_node,
        source_file_name="inv.xlsx",
        source_file_type="xlsx",
        status=status,
        normalized_data={
            "vendor_invoice_number": "INV-001",
            "invoice_date": "2026-04-01",
            "total_amount": 10000,
            "currency": "INR",
        },
    )


def _make_published_template(scope_node, approver, role, code="tarun-wf"):
    """
    Create a WorkflowTemplate with one NORMAL_APPROVAL step and publish it.
    Returns (template, published_version).
    """
    template = WorkflowTemplate.objects.create(
        scope_node=scope_node,
        name="Tarun Template",
        code=code,
        module="invoice",
        is_active=True,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template,
        version_number=1,
        status=VersionStatus.DRAFT,
    )
    group = StepGroup.objects.create(
        template_version=version,
        name="Review",
        display_order=1,
    )
    WorkflowStep.objects.create(
        group=group,
        name="Approve",
        display_order=1,
        required_role=role,
        step_kind=StepKind.NORMAL_APPROVAL,
        scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
        default_user=approver,
    )
    publish_template_version(version, published_by=approver)
    version.refresh_from_db()
    return template, version


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Route Org", code="route-org")


@pytest.fixture
def org2(db):
    return Organization.objects.create(name="Other Org", code="other-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/route-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/route-org/hq/ea", depth=1,
    )


@pytest.fixture
def approver_role(org):
    return Role.objects.create(org=org, name="Approver", code="approver")


@pytest.fixture
def approver(db, org, company, entity, approver_role):
    user = User.objects.create_user(email="approver@route.com", password="pass")
    from apps.access.services import assign_user_role
    assign_user_role(user, approver_role, company)
    assign_user_role(user, approver_role, entity)
    return user


@pytest.fixture
def vendor_user(db):
    return User.objects.create_user(email="vendor@route.com", password="pass")


@pytest.fixture
def vendor(org, company):
    return Vendor.objects.create(
        org=org,
        scope_node=company,
        vendor_name="ACME Corp",
        sap_vendor_id="SAP-001",
        operational_status=OperationalStatus.ACTIVE,
    )


@pytest.fixture
def vendor_assignment(vendor, vendor_user):
    return UserVendorAssignment.objects.create(
        user=vendor_user, vendor=vendor, is_active=True
    )


@pytest.fixture
def wf_template_and_version(company, approver, approver_role):
    return _make_published_template(company, approver, approver_role)


@pytest.fixture
def route(org, wf_template_and_version):
    template, _ = wf_template_and_version
    return VendorSubmissionRoute.objects.create(
        org=org,
        code="tarun",
        label="Tarun",
        workflow_template=template,
        is_active=True,
    )


@pytest.fixture
def submission(vendor, entity, vendor_user, vendor_assignment):
    return _make_submission(vendor, entity, vendor_user)


# ---------------------------------------------------------------------------
# A. Route config model
# ---------------------------------------------------------------------------

class TestRouteConfigModel:
    def test_route_created_with_required_fields(self, route, org, wf_template_and_version):
        template, _ = wf_template_and_version
        assert route.pk is not None
        assert route.org == org
        assert route.code == "tarun"
        assert route.label == "Tarun"
        assert route.workflow_template == template
        assert route.is_active is True

    def test_unique_code_per_org_constraint(self, route, org, wf_template_and_version):
        template, _ = wf_template_and_version
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            VendorSubmissionRoute.objects.create(
                org=org,
                code="tarun",  # duplicate
                label="Tarun Duplicate",
                workflow_template=template,
                is_active=True,
            )

    def test_inactive_route_excluded_from_vendor_list(
        self, org, wf_template_and_version, vendor, vendor_user, vendor_assignment, factory
    ):
        template, _ = wf_template_and_version
        VendorSubmissionRoute.objects.create(
            org=org, code="karun", label="Karun",
            workflow_template=template, is_active=False,
        )
        from apps.vendors.api.views import VendorSendToOptionsView
        view = VendorSendToOptionsView.as_view()
        request = factory.get("/vendors/vendor-send-to-options/")
        force_authenticate(request, user=vendor_user)
        response = view(request)
        assert response.status_code == 200
        codes = [r["code"] for r in response.data]
        assert "karun" not in codes

    def test_vendor_list_only_returns_own_org_routes(
        self, org, org2, wf_template_and_version, vendor, vendor_user, vendor_assignment,
        company, approver, factory
    ):
        template, _ = wf_template_and_version
        # Route for a completely different org — must not appear
        node2 = ScopeNode.objects.create(
            org=org2, parent=None, name="HQ2", code="hq2",
            node_type=NodeType.COMPANY, path="/other-org/hq2", depth=0,
        )
        other_template = WorkflowTemplate.objects.create(
            scope_node=node2, name="Other WF", code="other-wf",
            module="invoice", is_active=True,
        )
        VendorSubmissionRoute.objects.create(
            org=org2, code="aneish", label="Aneish",
            workflow_template=other_template, is_active=True,
        )
        from apps.vendors.api.views import VendorSendToOptionsView
        view = VendorSendToOptionsView.as_view()
        request = factory.get("/vendors/vendor-send-to-options/")
        force_authenticate(request, user=vendor_user)
        response = view(request)
        assert response.status_code == 200
        codes = [r["code"] for r in response.data]
        assert "aneish" not in codes


# ---------------------------------------------------------------------------
# B. Vendor submission requires send_to_option_id
# ---------------------------------------------------------------------------

class TestSubmitRequiresSendTo:
    def test_missing_send_to_returns_400(
        self, submission, vendor_user, vendor_assignment, factory
    ):
        view = VendorInvoiceSubmissionViewSet.as_view({"post": "submit_invoice"})
        request = factory.post(
            f"/vendor-invoice-submissions/{submission.pk}/submit/", {}, format="json"
        )
        force_authenticate(request, user=vendor_user)
        response = view(request, pk=submission.pk)
        assert response.status_code == 400
        assert "send_to_option_id" in str(response.data)

    def test_invalid_send_to_id_returns_400(
        self, submission, vendor_user, vendor_assignment, factory
    ):
        view = VendorInvoiceSubmissionViewSet.as_view({"post": "submit_invoice"})
        request = factory.post(
            f"/vendor-invoice-submissions/{submission.pk}/submit/",
            {"send_to_option_id": 99999},
            format="json",
        )
        force_authenticate(request, user=vendor_user)
        response = view(request, pk=submission.pk)
        assert response.status_code == 400

    def test_inactive_send_to_returns_400(
        self, org, submission, wf_template_and_version, vendor_user, vendor_assignment, factory
    ):
        template, _ = wf_template_and_version
        inactive = VendorSubmissionRoute.objects.create(
            org=org, code="inactive-route", label="Inactive",
            workflow_template=template, is_active=False,
        )
        view = VendorInvoiceSubmissionViewSet.as_view({"post": "submit_invoice"})
        request = factory.post(
            f"/vendor-invoice-submissions/{submission.pk}/submit/",
            {"send_to_option_id": inactive.pk},
            format="json",
        )
        force_authenticate(request, user=vendor_user)
        response = view(request, pk=submission.pk)
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# C. Auto-routing: invoice created, workflow activated
# ---------------------------------------------------------------------------

class TestAutoRouting:
    def test_submit_creates_invoice_and_activates_workflow(
        self, submission, route, vendor_user, vendor_assignment, factory
    ):
        view = VendorInvoiceSubmissionViewSet.as_view({"post": "submit_invoice"})
        request = factory.post(
            f"/vendor-invoice-submissions/{submission.pk}/submit/",
            {"send_to_option_id": route.pk},
            format="json",
        )
        force_authenticate(request, user=vendor_user)
        response = view(request, pk=submission.pk)
        assert response.status_code == 200, response.data

        invoice_id = response.data["invoice_id"]
        from apps.invoices.models import Invoice
        invoice = Invoice.objects.get(pk=invoice_id)

        # Invoice linked to selected workflow version
        assert invoice.selected_workflow_version_id is not None
        assert invoice.selected_workflow_template == route.workflow_template

        # Invoice status is IN_REVIEW (set by _sync_subject_status_on_workflow_change)
        assert invoice.status == InvoiceStatus.IN_REVIEW

        # Workflow instance created and active
        instance = WorkflowInstance.objects.get(subject_type="invoice", subject_id=invoice.pk)
        assert instance.status == InstanceStatus.ACTIVE

    def test_submission_status_becomes_submitted(
        self, submission, route, vendor_user, vendor_assignment, factory
    ):
        view = VendorInvoiceSubmissionViewSet.as_view({"post": "submit_invoice"})
        request = factory.post(
            f"/vendor-invoice-submissions/{submission.pk}/submit/",
            {"send_to_option_id": route.pk},
            format="json",
        )
        force_authenticate(request, user=vendor_user)
        view(request, pk=submission.pk)
        submission.refresh_from_db()
        assert submission.status == VendorInvoiceSubmissionStatus.SUBMITTED
        assert submission.send_to_route == route

    def test_submit_service_directly(self, submission, route, vendor_user, vendor_assignment):
        invoice = submit_vendor_invoice_with_route(
            submission, user=vendor_user, send_to_route=route
        )
        assert invoice.pk is not None
        assert invoice.selected_workflow_version_id is not None
        assert invoice.status == InvoiceStatus.IN_REVIEW
        instance = WorkflowInstance.objects.get(subject_type="invoice", subject_id=invoice.pk)
        assert instance.status == InstanceStatus.ACTIVE


# ---------------------------------------------------------------------------
# D. Misconfiguration blocking
# ---------------------------------------------------------------------------

class TestMisconfigurationBlocking:
    def test_route_with_no_published_version_blocks_submission(
        self, org, submission, vendor_user, vendor_assignment, approver_role
    ):
        template = WorkflowTemplate.objects.create(
            scope_node=submission.scope_node,
            name="Unpublished WF",
            code="unpub-wf",
            module="invoice",
            is_active=True,
        )
        # No published version created
        route = VendorSubmissionRoute.objects.create(
            org=org, code="broken", label="Broken",
            workflow_template=template, is_active=True,
        )
        with pytest.raises(VendorRouteError, match="no published version"):
            submit_vendor_invoice_with_route(submission, user=vendor_user, send_to_route=route)

    def test_route_with_no_published_version_leaves_no_orphan_invoice(
        self, org, submission, vendor_user, vendor_assignment
    ):
        from apps.invoices.models import Invoice
        template = WorkflowTemplate.objects.create(
            scope_node=submission.scope_node,
            name="Orphan WF",
            code="orphan-wf",
            module="invoice",
            is_active=True,
        )
        route = VendorSubmissionRoute.objects.create(
            org=org, code="orphan", label="Orphan",
            workflow_template=template, is_active=True,
        )
        before_count = Invoice.objects.count()
        with pytest.raises(VendorRouteError):
            submit_vendor_invoice_with_route(submission, user=vendor_user, send_to_route=route)
        assert Invoice.objects.count() == before_count

    def test_route_with_inactive_template_blocks_submission(
        self, org, submission, vendor_user, vendor_assignment
    ):
        template = WorkflowTemplate.objects.create(
            scope_node=submission.scope_node,
            name="Inactive WF",
            code="inactive-wf",
            module="invoice",
            is_active=False,  # inactive
        )
        route = VendorSubmissionRoute.objects.create(
            org=org, code="inactive-tmpl", label="Inactive Template",
            workflow_template=template, is_active=True,
        )
        with pytest.raises(VendorRouteError, match="not active"):
            submit_vendor_invoice_with_route(submission, user=vendor_user, send_to_route=route)

    def test_route_with_unresolvable_assignee_blocks_submission_atomically(
        self, org, submission, vendor_user, vendor_assignment, approver, approver_role
    ):
        """
        Template step has no default_user and no role-eligible users → NO_ELIGIBLE_USERS.
        activate_workflow_instance raises ValueError → VendorRouteError → entire transaction
        rolls back, leaving no orphan invoice or workflow instance.
        """
        from apps.invoices.models import Invoice

        # Role with no users assigned anywhere
        empty_role = Role.objects.create(
            org=org, name="Empty Role", code="empty-role"
        )
        template = WorkflowTemplate.objects.create(
            scope_node=submission.scope_node,
            name="No Assignee WF",
            code="no-assign-wf",
            module="invoice",
            is_active=True,
        )
        version = WorkflowTemplateVersion.objects.create(
            template=template, version_number=1, status=VersionStatus.DRAFT,
        )
        group = StepGroup.objects.create(
            template_version=version, name="Review", display_order=1,
        )
        WorkflowStep.objects.create(
            group=group, name="Approve", display_order=1,
            required_role=empty_role,          # role has no members at subject node
            step_kind=StepKind.NORMAL_APPROVAL,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            default_user=None,                  # no default either
        )
        publish_template_version(version, published_by=approver)

        route = VendorSubmissionRoute.objects.create(
            org=org, code="no-assign", label="No Assignee",
            workflow_template=template, is_active=True,
        )
        before_count = Invoice.objects.count()
        with pytest.raises(VendorRouteError, match="cannot start"):
            submit_vendor_invoice_with_route(submission, user=vendor_user, send_to_route=route)

        # Atomic: invoice must not have been created
        assert Invoice.objects.count() == before_count
        # Submission status unchanged
        submission.refresh_from_db()
        assert submission.status == VendorInvoiceSubmissionStatus.READY

    def test_inactive_route_raises_vendor_route_error(
        self, route, submission, vendor_user, vendor_assignment
    ):
        route.is_active = False
        route.save(update_fields=["is_active"])
        with pytest.raises(VendorRouteError, match="not active"):
            submit_vendor_invoice_with_route(submission, user=vendor_user, send_to_route=route)


# ---------------------------------------------------------------------------
# E. Scope correctness
# ---------------------------------------------------------------------------

class TestScopeCorrectness:
    def test_vendor_cannot_use_route_from_different_org_via_view(
        self, org2, wf_template_and_version, submission, vendor_user, vendor_assignment,
        company, factory
    ):
        """Route belonging to org2 must not be usable by a vendor in org."""
        node2 = ScopeNode.objects.create(
            org=org2, parent=None, name="HQ2", code="hq2",
            node_type=NodeType.COMPANY, path="/other-org/hq2", depth=0,
        )
        other_template = WorkflowTemplate.objects.create(
            scope_node=node2, name="Other WF", code="other-wf2",
            module="invoice", is_active=True,
        )
        cross_route = VendorSubmissionRoute.objects.create(
            org=org2, code="cross", label="Cross",
            workflow_template=other_template, is_active=True,
        )
        view = VendorInvoiceSubmissionViewSet.as_view({"post": "submit_invoice"})
        request = factory.post(
            f"/vendor-invoice-submissions/{submission.pk}/submit/",
            {"send_to_option_id": cross_route.pk},
            format="json",
        )
        force_authenticate(request, user=vendor_user)
        response = view(request, pk=submission.pk)
        # Must be 400 — not found for this vendor's org
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# F. Permission gating & serializer validation (hardening)
# ---------------------------------------------------------------------------

class TestRoutePermissionGating:
    def test_vendor_user_sees_empty_list_in_internal_crud(
        self, route, vendor_user, vendor_assignment, factory
    ):
        """Vendor user has no internal scope assignments → queryset filters to empty list."""
        from apps.vendors.api.views import VendorSubmissionRouteViewSet
        view = VendorSubmissionRouteViewSet.as_view({"get": "list"})
        request = factory.get("/vendors/send-to-options/")
        force_authenticate(request, user=vendor_user)
        response = view(request)
        assert response.status_code == 200
        results = response.data.get("results", response.data)
        assert len(results) == 0

    def test_vendor_user_cannot_create_route(
        self, org, wf_template_and_version, vendor_user, vendor_assignment, factory
    ):
        """Vendor user has no actionable scopes → POST to internal CRUD returns 403."""
        template, _ = wf_template_and_version
        from apps.vendors.api.views import VendorSubmissionRouteViewSet
        view = VendorSubmissionRouteViewSet.as_view({"post": "create"})
        request = factory.post(
            "/vendors/send-to-options/",
            {
                "org": org.pk,
                "code": "vendor-created-route",
                "label": "Should Fail",
                "workflow_template": template.pk,
                "is_active": True,
            },
            format="json",
        )
        force_authenticate(request, user=vendor_user)
        response = view(request)
        assert response.status_code == 403

    def test_create_rejects_cross_org_template(
        self, org, org2, approver, company, factory
    ):
        """Serializer rejects a template whose scope_node belongs to a different org."""
        node2 = ScopeNode.objects.create(
            org=org2, parent=None, name="HQ-other", code="hq-other",
            node_type=NodeType.COMPANY, path="/other-org/hq-other", depth=0,
        )
        cross_tmpl = WorkflowTemplate.objects.create(
            scope_node=node2, name="Cross Org WF", code="cross-org-wf",
            module="invoice", is_active=True,
        )
        from apps.vendors.api.views import VendorSubmissionRouteViewSet
        view = VendorSubmissionRouteViewSet.as_view({"post": "create"})
        request = factory.post(
            "/vendors/send-to-options/",
            {
                "org": org.pk,
                "code": "cross-org-route",
                "label": "Cross Org",
                "workflow_template": cross_tmpl.pk,
            },
            format="json",
        )
        force_authenticate(request, user=approver)
        response = view(request)
        assert response.status_code == 400
        assert "workflow_template" in str(response.data)

    def test_create_rejects_non_invoice_template(
        self, org, approver, company, factory
    ):
        """Serializer rejects templates whose module is not 'invoice'."""
        bad_module_tmpl = WorkflowTemplate.objects.create(
            scope_node=company, name="Budget WF", code="budget-wf-gate",
            module="budget", is_active=True,
        )
        from apps.vendors.api.views import VendorSubmissionRouteViewSet
        view = VendorSubmissionRouteViewSet.as_view({"post": "create"})
        request = factory.post(
            "/vendors/send-to-options/",
            {
                "org": org.pk,
                "code": "bad-module-route",
                "label": "Bad Module",
                "workflow_template": bad_module_tmpl.pk,
            },
            format="json",
        )
        force_authenticate(request, user=approver)
        response = view(request)
        assert response.status_code == 400
        assert "workflow_template" in str(response.data)

    def test_update_rejects_non_invoice_template_reassignment(
        self, route, approver, company, factory
    ):
        """PATCH: reassigning route to a non-invoice template is rejected."""
        bad_tmpl = WorkflowTemplate.objects.create(
            scope_node=company, name="Non Invoice WF", code="non-invoice-wf-gate",
            module="vendor", is_active=True,
        )
        from apps.vendors.api.views import VendorSubmissionRouteViewSet
        view = VendorSubmissionRouteViewSet.as_view({"patch": "partial_update"})
        request = factory.patch(
            f"/vendors/send-to-options/{route.pk}/",
            {"workflow_template": bad_tmpl.pk},
            format="json",
        )
        force_authenticate(request, user=approver)
        response = view(request, pk=route.pk)
        assert response.status_code == 400
        assert "workflow_template" in str(response.data)

    def test_internal_user_can_create_and_update_route(
        self, org, wf_template_and_version, approver, factory
    ):
        """Internal user with valid scope assignment can create and update a route."""
        template, _ = wf_template_and_version
        from apps.vendors.api.views import VendorSubmissionRouteViewSet

        create_view = VendorSubmissionRouteViewSet.as_view({"post": "create"})
        create_req = factory.post(
            "/vendors/send-to-options/",
            {
                "org": org.pk,
                "code": "internal-route",
                "label": "Internal Route",
                "workflow_template": template.pk,
                "is_active": True,
            },
            format="json",
        )
        force_authenticate(create_req, user=approver)
        create_resp = create_view(create_req)
        assert create_resp.status_code == 201, create_resp.data
        new_route_id = create_resp.data["id"]

        patch_view = VendorSubmissionRouteViewSet.as_view({"patch": "partial_update"})
        patch_req = factory.patch(
            f"/vendors/send-to-options/{new_route_id}/",
            {"label": "Internal Route Updated"},
            format="json",
        )
        force_authenticate(patch_req, user=approver)
        patch_resp = patch_view(patch_req, pk=new_route_id)
        assert patch_resp.status_code == 200, patch_resp.data
        assert patch_resp.data["label"] == "Internal Route Updated"
