from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet

from apps.access.models import Role, Permission, RolePermission
from apps.access.api.serializers.roles import (
    RoleSerializer,
    PermissionSerializer,
    RolePermissionSerializer,
)


class RoleViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = RoleSerializer

    def get_queryset(self):
        qs = Role.objects.select_related("org").order_by("name")
        org_id = self.request.query_params.get("org")
        if org_id:
            qs = qs.filter(org_id=org_id)
        return qs


class PermissionViewSet(ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PermissionSerializer
    queryset = Permission.objects.all().order_by("resource", "action")


class RolePermissionViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = RolePermissionSerializer

    def get_queryset(self):
        qs = RolePermission.objects.select_related("role", "permission")
        role_id = self.request.query_params.get("role")
        if role_id:
            qs = qs.filter(role_id=role_id)
        return qs
