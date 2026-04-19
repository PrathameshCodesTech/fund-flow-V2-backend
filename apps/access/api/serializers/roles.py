from rest_framework import serializers
from apps.access.models import Role, Permission, RolePermission


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ("id", "org", "name", "code", "node_type_scope", "is_active", "created_at")
        read_only_fields = ("id", "created_at")


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ("id", "action", "resource", "description")
        read_only_fields = ("id",)


class RolePermissionSerializer(serializers.ModelSerializer):
    permission_detail = PermissionSerializer(source="permission", read_only=True)

    class Meta:
        model = RolePermission
        fields = ("id", "role", "permission", "permission_detail")
        read_only_fields = ("id",)
