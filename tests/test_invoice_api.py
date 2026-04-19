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
