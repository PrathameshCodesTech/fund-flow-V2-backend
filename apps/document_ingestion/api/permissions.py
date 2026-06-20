from django.conf import settings
from rest_framework.permissions import BasePermission

from apps.access.models import UserRoleAssignment


class IsDocumentIngestionOperator(BasePermission):
    message = "Finance or administrator access is required for document ingestion."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        finance_roles = set(getattr(settings, "FINANCE_ROLE_CODES", {"finance_team"}))
        allowed_roles = finance_roles | {"org_admin", "tenant_admin"}
        return UserRoleAssignment.objects.filter(
            user=user,
            role__code__in=allowed_roles,
            role__is_active=True,
        ).exists()

