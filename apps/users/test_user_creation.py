from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.access.capabilities import get_capabilities_for_role
from apps.access.models import Role, UserRoleAssignment
from apps.core.models import NodeType, Organization, ScopeNode
from apps.users.api.serializers.users import UserCreateSerializer
from apps.users.models import User


class UserCreationWithRoleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Horizon", code="horizon")
        self.scope = ScopeNode.objects.create(
            org=self.org,
            name="Marketing",
            code="marketing",
            node_type=NodeType.DEPARTMENT,
            path="/horizon/marketing",
            depth=0,
        )
        self.role = Role.objects.create(
            org=self.org,
            name="HOD",
            code="hod",
            node_type_scope=NodeType.DEPARTMENT,
        )

    def payload(self, **overrides):
        payload = {
            "email": "new.user@example.com",
            "first_name": "New",
            "last_name": "User",
            "role": self.role.id,
            "scope_node": self.scope.id,
        }
        payload.update(overrides)
        return payload

    def test_creates_user_and_role_assignment_together(self):
        serializer = UserCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)

        user = serializer.save()

        self.assertTrue(
            UserRoleAssignment.objects.filter(
                user=user,
                role=self.role,
                scope_node=self.scope,
            ).exists()
        )

    def test_rejects_role_from_another_organization(self):
        other_org = Organization.objects.create(name="Other", code="other")
        other_role = Role.objects.create(org=other_org, name="Other HOD", code="hod")
        serializer = UserCreateSerializer(data=self.payload(role=other_role.id))

        self.assertFalse(serializer.is_valid())
        self.assertIn("role", serializer.errors)
        self.assertFalse(User.objects.filter(email="new.user@example.com").exists())

    def test_rejects_role_at_incompatible_node_type(self):
        branch_role = Role.objects.create(
            org=self.org,
            name="Branch Manager",
            code="branch_manager",
            node_type_scope=NodeType.BRANCH,
        )
        serializer = UserCreateSerializer(data=self.payload(role=branch_role.id))

        self.assertFalse(serializer.is_valid())
        self.assertIn("scope_node", serializer.errors)

    def test_assignment_failure_rolls_back_user_creation(self):
        serializer = UserCreateSerializer(data=self.payload())
        self.assertTrue(serializer.is_valid(), serializer.errors)

        with patch.object(UserRoleAssignment.objects, "create", side_effect=RuntimeError("assignment failed")):
            with self.assertRaisesMessage(RuntimeError, "assignment failed"):
                serializer.save()

        self.assertFalse(User.objects.filter(email="new.user@example.com").exists())

    def test_create_api_returns_role_and_computed_capabilities(self):
        admin = User.objects.create_superuser(email="admin@example.com", password="test-password")
        client = APIClient()
        client.force_authenticate(admin)

        response = client.post("/api/v1/users/", self.payload(), format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["assigned_roles"], [{"code": "hod", "name": "HOD"}])
        self.assertTrue(get_capabilities_for_role("hod").issubset(set(response.data["capabilities"])))
