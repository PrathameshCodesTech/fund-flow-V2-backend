"""
API-level tests for the /api/v1/users/ endpoint.
"""
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework import status
from django.urls import reverse

from apps.users.models import User
from apps.users.api.views.users import UserViewSet


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def active_user(db):
    return User.objects.create_user(
        email="alice@example.com",
        password="pass",
        first_name="Alice",
        last_name="Smith",
        is_active=True,
    )


@pytest.fixture
def inactive_user(db):
    return User.objects.create_user(
        email="bob@example.com",
        password="pass",
        first_name="Bob",
        last_name="Jones",
        is_active=False,
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        email="admin@example.com",
        password="pass",
    )


class TestUserList:
    def test_authenticated_user_can_list(self, factory, active_user, inactive_user, admin_user):
        """Authenticated user receives paginated user list."""
        request = factory.get("/api/v1/users/")
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        assert "results" in response.data
        # Authenticated user (alice) must be in the list
        emails = [u["email"] for u in response.data["results"]]
        assert "alice@example.com" in emails

    def test_unauthenticated_returns_401_or_403(self, factory):
        """Unauthenticated request is rejected (401 when auth required, 403 when denied)."""
        request = factory.get("/api/v1/users/")
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        # DRF may return 401 (no credentials) or 403 (credentials provided but invalid)
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_search_by_email(self, factory, active_user, inactive_user):
        """Search returns matching users by email."""
        request = factory.get("/api/v1/users/", {"q": "alice"})
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        emails = [u["email"] for u in response.data["results"]]
        assert "alice@example.com" in emails
        assert "bob@example.com" not in emails

    def test_search_by_first_name(self, factory, active_user, inactive_user):
        """Search returns matching users by first name."""
        request = factory.get("/api/v1/users/", {"q": "Bob"})
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        emails = [u["email"] for u in response.data["results"]]
        assert "bob@example.com" in emails
        assert "alice@example.com" not in emails

    def test_search_by_last_name(self, factory, active_user, inactive_user):
        """Search returns matching users by last name."""
        request = factory.get("/api/v1/users/", {"q": "Jones"})
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        emails = [u["email"] for u in response.data["results"]]
        assert "bob@example.com" in emails

    def test_filter_is_active_true(self, factory, active_user, inactive_user):
        """is_active=true returns only active users."""
        request = factory.get("/api/v1/users/", {"is_active": "true"})
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        emails = [u["email"] for u in response.data["results"]]
        assert "alice@example.com" in emails
        assert "bob@example.com" not in emails

    def test_filter_is_active_false(self, factory, active_user, inactive_user):
        """is_active=false returns only inactive users."""
        request = factory.get("/api/v1/users/", {"is_active": "false"})
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        emails = [u["email"] for u in response.data["results"]]
        assert "bob@example.com" in emails
        assert "alice@example.com" not in emails

    def test_no_password_in_response(self, factory, active_user):
        """Serializer never exposes password field."""
        request = factory.get("/api/v1/users/")
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        for user_data in response.data["results"]:
            assert "password" not in user_data
            assert "is_superuser" not in user_data
            assert "last_login" not in user_data  # not in serializer fields

    def test_user_detail(self, factory, active_user):
        """GET /users/{id}/ returns single user."""
        request = factory.get(f"/api/v1/users/{active_user.id}/")
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"get": "retrieve"})
        response = view(request, pk=active_user.id)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["email"] == "alice@example.com"
        assert response.data["first_name"] == "Alice"
        assert "password" not in response.data


# ── User Create ────────────────────────────────────────────────────────────────

class TestUserCreate:
    def test_admin_can_create_user(self, factory, admin_user):
        """Admin can create a new user."""
        request = factory.post("/api/v1/users/", {
            "email": "newperson@example.com",
            "first_name": "New",
            "last_name": "Person",
        }, format="json")
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"post": "create"})
        response = view(request)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["email"] == "newperson@example.com"
        assert response.data["first_name"] == "New"
        assert response.data["last_name"] == "Person"
        assert "password" not in response.data
        assert response.data["is_active"] is True
        assert "id" in response.data

    def test_admin_can_create_user_with_employee_id(self, factory, admin_user):
        """Admin can create a user with an employee_id."""
        request = factory.post("/api/v1/users/", {
            "email": "emp@example.com",
            "first_name": "Worker",
            "last_name": "Bee",
            "employee_id": "EMP-001",
        }, format="json")
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"post": "create"})
        response = view(request)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["employee_id"] == "EMP-001"

    def test_create_rejects_duplicate_email(self, factory, admin_user, active_user):
        """Duplicate email is rejected."""
        request = factory.post("/api/v1/users/", {
            "email": active_user.email,
            "first_name": "Duplicate",
            "last_name": "Email",
        }, format="json")
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"post": "create"})
        response = view(request)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "email" in response.data

    def test_create_rejects_duplicate_employee_id(self, factory, admin_user, active_user):
        """Duplicate employee_id is rejected."""
        request = factory.post("/api/v1/users/", {
            "email": "other@example.com",
            "first_name": "Other",
            "last_name": "Person",
            "employee_id": "EMP-TAKEN",
        }, format="json")
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"post": "create"})
        response = view(request)
        assert response.status_code == status.HTTP_201_CREATED

        # Second user with same employee_id
        request2 = factory.post("/api/v1/users/", {
            "email": "another@example.com",
            "first_name": "Another",
            "last_name": "Person",
            "employee_id": "EMP-TAKEN",
        }, format="json")
        force_authenticate(request2, user=admin_user)
        response2 = view(request2)
        assert response2.status_code == status.HTTP_400_BAD_REQUEST
        assert "employee_id" in response2.data

    def test_non_admin_cannot_create_user(self, factory, active_user):
        """Non-admin user receives 403 on create."""
        request = factory.post("/api/v1/users/", {
            "email": "hack@example.com",
            "first_name": "Hacker",
            "last_name": "Bad",
        }, format="json")
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"post": "create"})
        response = view(request)
        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_401_UNAUTHORIZED,
        )


# ── User Update / Deactivate ──────────────────────────────────────────────────

class TestUserUpdate:
    def test_admin_can_patch_user(self, factory, admin_user, active_user):
        """Admin can PATCH first_name, last_name, employee_id."""
        request = factory.patch(
            f"/api/v1/users/{active_user.id}/",
            {"first_name": "Alice", "last_name": "Updated", "employee_id": "EMP-UPD"},
            format="json",
        )
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"patch": "partial_update"})
        response = view(request, pk=active_user.id)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["first_name"] == "Alice"
        assert response.data["last_name"] == "Updated"
        assert response.data["employee_id"] == "EMP-UPD"

    def test_admin_can_deactivate_user(self, factory, admin_user, active_user):
        """PATCH is_active=false deactivates the user."""
        request = factory.patch(
            f"/api/v1/users/{active_user.id}/",
            {"is_active": False},
            format="json",
        )
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"patch": "partial_update"})
        response = view(request, pk=active_user.id)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["is_active"] is False

        # Confirm deactivated user cannot be found via is_active filter
        request2 = factory.get("/api/v1/users/", {"is_active": "true"})
        force_authenticate(request2, user=admin_user)
        view2 = UserViewSet.as_view({"get": "list"})
        resp2 = view2(request2)
        emails = [u["email"] for u in resp2.data["results"]]
        assert active_user.email not in emails

    def test_admin_can_reactivate_user(self, factory, admin_user, inactive_user):
        """PATCH is_active=true reactivates an inactive user."""
        request = factory.patch(
            f"/api/v1/users/{inactive_user.id}/",
            {"is_active": True},
            format="json",
        )
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"patch": "partial_update"})
        response = view(request, pk=inactive_user.id)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["is_active"] is True

    def test_non_admin_cannot_patch_user(self, factory, active_user, inactive_user):
        """Non-admin receives 403 on PATCH."""
        request = factory.patch(
            f"/api/v1/users/{inactive_user.id}/",
            {"is_active": False},
            format="json",
        )
        force_authenticate(request, user=active_user)
        view = UserViewSet.as_view({"patch": "partial_update"})
        response = view(request, pk=inactive_user.id)
        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_email_not_changeable_via_patch(self, factory, admin_user, active_user):
        """Email field is read-only on update."""
        request = factory.patch(
            f"/api/v1/users/{active_user.id}/",
            {"email": "changed@example.com"},
            format="json",
        )
        force_authenticate(request, user=admin_user)
        view = UserViewSet.as_view({"patch": "partial_update"})
        response = view(request, pk=active_user.id)
        assert response.status_code == status.HTTP_200_OK
        # email should remain unchanged
        assert response.data["email"] == active_user.email


# ── Auth/Me still works ───────────────────────────────────────────────────────

class TestAuthMe:
    def test_me_returns_current_user(self, factory, active_user):
        """GET /auth/me/ returns the authenticated user's own data."""
        from apps.users.api.views.auth import MeView
        request = factory.get("/api/v1/auth/me/")
        force_authenticate(request, user=active_user)
        view = MeView.as_view()
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["email"] == active_user.email
        assert response.data["first_name"] == active_user.first_name

    def test_login_still_works(self, factory, active_user):
        """POST /auth/login/ returns tokens + user data."""
        from apps.users.api.views.auth import LoginView
        request = factory.post("/api/v1/auth/login/", {
            "email": "alice@example.com",
            "password": "pass",
        }, format="json")
        view = LoginView.as_view()
        response = view(request)
        assert response.status_code == status.HTTP_200_OK
        assert "access" in response.data
        assert "refresh" in response.data
        assert response.data["user"]["email"] == "alice@example.com"
