import pytest
from apps.core.models import Organization, ScopeNode, NodeType
from apps.access.models import Role, Permission, PermissionAction, PermissionResource
from apps.users.models import User
from apps.access.services import (
    assign_user_to_scope,
    assign_user_role,
    grant_permission_to_role,
    user_has_permission_at_node,
    user_has_permission_including_ancestors,
)


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Org", code="ac-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/ac-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/ac-org/hq/ea", depth=1,
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="user@example.com", password="pass")


@pytest.fixture
def role(org):
    return Role.objects.create(org=org, name="Finance", code="finance")


@pytest.fixture
def permission(db):
    return Permission.objects.create(
        action=PermissionAction.APPROVE,
        resource=PermissionResource.INVOICE,
    )


class TestPermissionAtNode:
    def test_no_role_no_permission(self, user, entity, role, permission):
        assert not user_has_permission_at_node(
            user, PermissionAction.APPROVE, PermissionResource.INVOICE, entity
        )

    def test_with_role_and_permission(self, user, entity, role, permission):
        grant_permission_to_role(role, permission)
        assign_user_role(user, role, entity)
        assert user_has_permission_at_node(
            user, PermissionAction.APPROVE, PermissionResource.INVOICE, entity
        )

    def test_role_at_different_node_does_not_grant(self, user, entity, company, role, permission):
        grant_permission_to_role(role, permission)
        assign_user_role(user, role, company)
        assert not user_has_permission_at_node(
            user, PermissionAction.APPROVE, PermissionResource.INVOICE, entity
        )


class TestPermissionIncludingAncestors:
    def test_permission_at_ancestor(self, user, company, entity, role, permission):
        grant_permission_to_role(role, permission)
        assign_user_role(user, role, company)
        assert user_has_permission_including_ancestors(
            user, PermissionAction.APPROVE, PermissionResource.INVOICE, entity
        )

    def test_no_permission_anywhere(self, user, entity, role, permission):
        assert not user_has_permission_including_ancestors(
            user, PermissionAction.APPROVE, PermissionResource.INVOICE, entity
        )
