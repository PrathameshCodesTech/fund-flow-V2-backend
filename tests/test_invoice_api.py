"""
API-level tests for invoice authorization.
Verifies that the InvoiceViewSet correctly enforces permission-scoped access.
"""
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate
from apps.invoices.models import Invoice, InvoiceStatus
from apps.invoices.api.views import InvoiceViewSet
from apps.access.models import Role, Permission, PermissionAction, PermissionResource, UserRoleAssignment
from apps.access.services import grant_permission_to_role, assign_user_role
from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="API Org", code="api-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/api-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/api-org/hq/ea", depth=1,
    )


@pytest.fixture
def read_permission(db):
    return Permission.objects.get_or_create(
        action=PermissionAction.READ,
        resource=PermissionResource.INVOICE,
    )[0]


@pytest.fixture
def create_permission(db):
    return Permission.objects.get_or_create(
        action=PermissionAction.CREATE,
        resource=PermissionResource.INVOICE,
    )[0]


@pytest.fixture
def owner_user(db):
    return User.objects.create_user(email="owner@example.com", password="pass")


@pytest.fixture
def permitted_user(db):
    return User.objects.create_user(email="permitted@example.com", password="pass")


@pytest.fixture
def ancestor_permitted_user(db):
    return User.objects.create_user(email="ancestor@example.com", password="pass")


@pytest.fixture
def no_access_user(db):
    return User.objects.create_user(email="noaccess@example.com", password="pass")


@pytest.fixture
def reader_role(org, read_permission):
    role = Role.objects.create(org=org, name="Invoice Reader", code="reader")
    grant_permission_to_role(role, read_permission)
    return role


@pytest.fixture
def creator_role(org, create_permission):
    role = Role.objects.create(org=org, name="Invoice Creator", code="creator")
    grant_permission_to_role(role, create_permission)
    return role


@pytest.fixture
def invoice_at_entity(org, entity, owner_user):
    return Invoice.objects.create(
        title="Test Invoice", amount="1000.00", currency="INR",
        scope_node=entity, created_by=owner_user, status=InvoiceStatus.DRAFT,
    )


@pytest.fixture
def _permitted_at_entity(permitted_user, reader_role, entity):
    assign_user_role(permitted_user, reader_role, entity)


@pytest.fixture
def _permitted_at_ancestor(ancestor_permitted_user, reader_role, company):
    assign_user_role(ancestor_permitted_user, reader_role, company)


def _make_request(factory, method, path, user, data=None):
    fn = getattr(factory, method)
    request = fn(path, data, format="json") if data else fn(path)
    force_authenticate(request, user=user)
    return request


class TestInvoiceListAuthorization:
    def test_permitted_user_at_exact_node_sees_invoice(self, factory, invoice_at_entity, permitted_user, _permitted_at_entity):
        request = _make_request(factory, "get", "/invoices/", permitted_user)
        view = InvoiceViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200, f"got {response.status_code}: {response.data}"
        results = response.data.get('results', response.data)
        invoice_ids = [i["id"] for i in results]
        assert invoice_at_entity.pk in invoice_ids

    def test_permitted_user_at_ancestor_sees_invoice(self, factory, invoice_at_entity, ancestor_permitted_user, _permitted_at_ancestor):
        request = _make_request(factory, "get", "/invoices/", ancestor_permitted_user)
        view = InvoiceViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200
        results = response.data.get('results', response.data)
        invoice_ids = [i["id"] for i in results]
        assert invoice_at_entity.pk in invoice_ids

    def test_no_permission_user_does_not_see_invoice(self, factory, invoice_at_entity, no_access_user):
        request = _make_request(factory, "get", "/invoices/", no_access_user)
        view = InvoiceViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200
        results = response.data.get('results', response.data)
        invoice_ids = [i["id"] for i in results]
        assert invoice_at_entity.pk not in invoice_ids

    def test_creator_sees_own_invoice_without_permission(self, factory, invoice_at_entity, owner_user):
        request = _make_request(factory, "get", "/invoices/", owner_user)
        view = InvoiceViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200
        results = response.data.get('results', response.data)
        invoice_ids = [i["id"] for i in results]
        assert invoice_at_entity.pk in invoice_ids


class TestInvoiceDetailAuthorization:
    def test_permitted_user_can_retrieve(self, factory, invoice_at_entity, permitted_user, _permitted_at_entity):
        request = _make_request(factory, "get", f"/invoices/{invoice_at_entity.pk}/", permitted_user)
        view = InvoiceViewSet.as_view({"get": "retrieve"})
        response = view(request, pk=invoice_at_entity.pk)
        assert response.status_code == 200, f"got {response.status_code}"
        assert response.data["id"] == invoice_at_entity.pk

    def test_no_permission_user_gets_404(self, factory, invoice_at_entity, no_access_user):
        request = _make_request(factory, "get", f"/invoices/{invoice_at_entity.pk}/", no_access_user)
        view = InvoiceViewSet.as_view({"get": "retrieve"})
        response = view(request, pk=invoice_at_entity.pk)
        # REST semantics: 404 so unauthorized users cannot enumerate resources
        assert response.status_code == 404

    def test_creator_can_retrieve_without_permission(self, factory, invoice_at_entity, owner_user):
        request = _make_request(factory, "get", f"/invoices/{invoice_at_entity.pk}/", owner_user)
        view = InvoiceViewSet.as_view({"get": "retrieve"})
        response = view(request, pk=invoice_at_entity.pk)
        assert response.status_code == 200


class TestInvoiceCreate:
    def test_user_with_create_permission_can_create(self, factory, entity, permitted_user, reader_role, create_permission):
        """User with CREATE permission can create invoices."""
        grant_permission_to_role(reader_role, create_permission)
        assign_user_role(permitted_user, reader_role, entity)
        data = {
            "scope_node": entity.pk,
            "title": "New Invoice",
            "amount": "500.00",
            "currency": "INR",
        }
        request = _make_request(factory, "post", "/invoices/", permitted_user, data)
        view = InvoiceViewSet.as_view({"post": "create"})
        response = view(request)
        assert response.status_code == 201, f"got {response.status_code}: {response.data}"
        assert response.data["title"] == "New Invoice"

    def test_user_without_create_permission_gets_403(self, factory, entity, no_access_user):
        data = {
            "scope_node": entity.pk,
            "title": "New Invoice",
            "amount": "500.00",
            "currency": "INR",
        }
        request = _make_request(factory, "post", "/invoices/", no_access_user, data)
        view = InvoiceViewSet.as_view({"post": "create"})
        response = view(request)
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tests: eligible-workflows multi-variant behavior
# ---------------------------------------------------------------------------

class TestEligibleWorkflowsMultiVariant:
    """
    Verify that eligible-workflows returns all active published variants
    and excludes inactive templates.
    """

    @pytest.fixture
    def invoice_pw(self, entity, owner_user):
        """Invoice in PENDING_WORKFLOW state."""
        from apps.invoices.models import InvoiceStatus
        return Invoice.objects.create(
            title="WF Test Invoice", amount="2000.00", currency="INR",
            scope_node=entity, created_by=owner_user,
            status=InvoiceStatus.PENDING_WORKFLOW,
        )

    def _eligible(self, factory, user, invoice):
        request = _make_request(factory, "get", f"/invoices/{invoice.pk}/eligible-workflows/", user)
        view = InvoiceViewSet.as_view({"get": "eligible_workflows"})
        return view(request, pk=invoice.pk)

    def test_both_active_published_variants_returned(
        self, factory, entity, owner_user, invoice_pw
    ):
        """Both active published template variants appear in eligible-workflows."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus

        t1 = WorkflowTemplate.objects.create(
            name="Standard", module="invoice", scope_node=entity,
            created_by=owner_user, code="standard", is_active=True,
        )
        t2 = WorkflowTemplate.objects.create(
            name="Fast Track", module="invoice", scope_node=entity,
            created_by=owner_user, code="fast-track", is_active=True,
        )
        WorkflowTemplateVersion.objects.create(template=t1, version_number=1, status=VersionStatus.PUBLISHED)
        WorkflowTemplateVersion.objects.create(template=t2, version_number=1, status=VersionStatus.PUBLISHED)

        response = self._eligible(factory, owner_user, invoice_pw)
        assert response.status_code == 200
        template_codes = {r["template_code"] for r in response.data}
        assert "standard" in template_codes
        assert "fast-track" in template_codes

    def test_inactive_template_excluded_from_eligible_workflows(
        self, factory, entity, owner_user, invoice_pw
    ):
        """An inactive template does not appear in eligible-workflows."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus

        t_active = WorkflowTemplate.objects.create(
            name="Active", module="invoice", scope_node=entity,
            created_by=owner_user, code="active", is_active=True,
        )
        t_inactive = WorkflowTemplate.objects.create(
            name="Inactive", module="invoice", scope_node=entity,
            created_by=owner_user, code="inactive-wf", is_active=False,
        )
        WorkflowTemplateVersion.objects.create(template=t_active, version_number=1, status=VersionStatus.PUBLISHED)
        WorkflowTemplateVersion.objects.create(template=t_inactive, version_number=1, status=VersionStatus.PUBLISHED)

        response = self._eligible(factory, owner_user, invoice_pw)
        assert response.status_code == 200
        template_codes = {r["template_code"] for r in response.data}
        assert "active" in template_codes
        assert "inactive-wf" not in template_codes

    def test_eligible_workflows_response_includes_template_code(
        self, factory, entity, owner_user, invoice_pw
    ):
        """Each entry in eligible-workflows includes template_code."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus

        t = WorkflowTemplate.objects.create(
            name="Test Template", module="invoice", scope_node=entity,
            created_by=owner_user, code="test-template", is_active=True,
        )
        WorkflowTemplateVersion.objects.create(template=t, version_number=1, status=VersionStatus.PUBLISHED)

        response = self._eligible(factory, owner_user, invoice_pw)
        assert response.status_code == 200
        assert len(response.data) >= 1
        for entry in response.data:
            assert "template_code" in entry

    def test_attach_workflow_works_for_either_variant(
        self, factory, entity, owner_user, invoice_pw
    ):
        """attach-workflow can attach a published version from any active variant."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus
        from apps.access.models import Role, UserRoleAssignment
        from apps.workflow.models import StepGroup, WorkflowStep, ParallelMode, RejectionAction, ScopeResolutionPolicy

        approver_role = Role.objects.create(org=entity.org, name="Approver", code="approver-av")

        t_fast = WorkflowTemplate.objects.create(
            name="Fast Track", module="invoice", scope_node=entity,
            created_by=owner_user, code="fast-track-av", is_active=True,
        )
        v_fast = WorkflowTemplateVersion.objects.create(
            template=t_fast, version_number=1, status=VersionStatus.PUBLISHED
        )
        grp = StepGroup.objects.create(
            template_version=v_fast, name="G1", display_order=1,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=grp, name="Step 1", required_role=approver_role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        UserRoleAssignment.objects.create(user=owner_user, role=approver_role, scope_node=entity)

        request = _make_request(
            factory, "post", f"/invoices/{invoice_pw.pk}/attach-workflow/",
            owner_user, {"template_version_id": v_fast.pk}
        )
        view = InvoiceViewSet.as_view({"post": "attach_workflow"})
        response = view(request, pk=invoice_pw.pk)
        assert response.status_code == 201, f"got {response.status_code}: {response.data}"
        assert response.data["workflow_instance"]["template_version_id"] == v_fast.pk

    def test_attach_workflow_rejects_inactive_template(
        self, factory, entity, owner_user, invoice_pw
    ):
        """attach-workflow returns 400 when the selected template is inactive."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus

        t_inactive = WorkflowTemplate.objects.create(
            name="Inactive", module="invoice", scope_node=entity,
            created_by=owner_user, code="inactive-attach", is_active=False,
        )
        v = WorkflowTemplateVersion.objects.create(
            template=t_inactive, version_number=1, status=VersionStatus.PUBLISHED
        )

        request = _make_request(
            factory, "post", f"/invoices/{invoice_pw.pk}/attach-workflow/",
            owner_user, {"template_version_id": v.pk}
        )
        view = InvoiceViewSet.as_view({"post": "attach_workflow"})
        response = view(request, pk=invoice_pw.pk)
        assert response.status_code == 400
        assert "not active" in response.data["detail"]

    def test_attach_workflow_rejects_unrelated_scope_template(
        self, factory, org, entity, owner_user, invoice_pw
    ):
        """attach-workflow returns 400 when the template belongs to a scope outside the invoice's chain."""
        from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus
        from apps.core.models import ScopeNode, NodeType

        # Create a completely separate branch node not in the invoice's ancestor chain
        other_entity = ScopeNode.objects.create(
            org=org, parent=None, name="Other HQ", code="other-hq",
            node_type=NodeType.COMPANY, path="/api-org/other-hq", depth=0,
        )
        t_other = WorkflowTemplate.objects.create(
            name="Other Scope", module="invoice", scope_node=other_entity,
            created_by=owner_user, code="other-scope", is_active=True,
        )
        v = WorkflowTemplateVersion.objects.create(
            template=t_other, version_number=1, status=VersionStatus.PUBLISHED
        )

        request = _make_request(
            factory, "post", f"/invoices/{invoice_pw.pk}/attach-workflow/",
            owner_user, {"template_version_id": v.pk}
        )
        view = InvoiceViewSet.as_view({"post": "attach_workflow"})
        response = view(request, pk=invoice_pw.pk)
        assert response.status_code == 400
        assert "ancestor" in response.data["detail"]

    def test_attach_workflow_accepts_ancestor_scope_template(
        self, factory, company, entity, owner_user, invoice_pw
    ):
        """attach-workflow accepts a template configured at an ancestor scope node."""
        from apps.workflow.models import (
            WorkflowTemplate, WorkflowTemplateVersion, VersionStatus,
            StepGroup, WorkflowStep, ParallelMode, RejectionAction, ScopeResolutionPolicy,
        )
        from apps.access.models import Role, UserRoleAssignment

        approver_role = Role.objects.create(org=entity.org, name="Approver Anc", code="approver-anc")

        # Template is configured at company (ancestor of entity)
        t_anc = WorkflowTemplate.objects.create(
            name="Company Level", module="invoice", scope_node=company,
            created_by=owner_user, code="company-level", is_active=True,
        )
        v = WorkflowTemplateVersion.objects.create(
            template=t_anc, version_number=1, status=VersionStatus.PUBLISHED
        )
        grp = StepGroup.objects.create(
            template_version=v, name="G1", display_order=1,
            parallel_mode=ParallelMode.SINGLE,
            on_rejection_action=RejectionAction.TERMINATE,
        )
        WorkflowStep.objects.create(
            group=grp, name="Step 1", required_role=approver_role,
            scope_resolution_policy=ScopeResolutionPolicy.SUBJECT_NODE,
            display_order=1,
        )
        UserRoleAssignment.objects.create(user=owner_user, role=approver_role, scope_node=entity)

        request = _make_request(
            factory, "post", f"/invoices/{invoice_pw.pk}/attach-workflow/",
            owner_user, {"template_version_id": v.pk}
        )
        view = InvoiceViewSet.as_view({"post": "attach_workflow"})
        response = view(request, pk=invoice_pw.pk)
        assert response.status_code == 201, f"got {response.status_code}: {response.data}"


class TestWorkflowTemplateCodeStability:
    """Verify that code is auto-generated only on create and stable across renames."""

    def test_code_auto_generated_on_create_when_omitted(self, factory, entity, owner_user):
        """Creating a template without code auto-generates it from the name."""
        from apps.workflow.api.serializers.templates import WorkflowTemplateSerializer

        data = {
            "name": "My Invoice Flow",
            "module": "invoice",
            "scope_node": entity.pk,
        }
        serializer = WorkflowTemplateSerializer(data=data)
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["code"] == "my-invoice-flow"

    def test_rename_does_not_mutate_code_when_code_omitted(self, factory, entity, owner_user):
        """PATCH name without sending code leaves code unchanged."""
        from apps.workflow.models import WorkflowTemplate
        from apps.workflow.api.serializers.templates import WorkflowTemplateSerializer

        template = WorkflowTemplate.objects.create(
            name="Original Name", module="invoice", scope_node=entity,
            created_by=owner_user, code="original-code",
        )
        # PATCH: only send name, no code
        serializer = WorkflowTemplateSerializer(
            template, data={"name": "Renamed Flow"}, partial=True
        )
        assert serializer.is_valid(), serializer.errors
        # code must NOT change — original-code remains
        assert "code" not in serializer.validated_data or serializer.validated_data.get("code") == "original-code"

    def test_explicit_code_in_update_is_respected(self, factory, entity, owner_user):
        """PATCH that explicitly sends a new code updates the code."""
        from apps.workflow.models import WorkflowTemplate
        from apps.workflow.api.serializers.templates import WorkflowTemplateSerializer

        template = WorkflowTemplate.objects.create(
            name="Old Name", module="invoice", scope_node=entity,
            created_by=owner_user, code="old-code",
        )
        serializer = WorkflowTemplateSerializer(
            template, data={"name": "Old Name", "code": "new-code"}, partial=True
        )
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["code"] == "new-code"
