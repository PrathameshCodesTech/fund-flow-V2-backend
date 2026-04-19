from rest_framework import serializers
from apps.access.models import UserScopeAssignment, UserRoleAssignment


class UserScopeAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserScopeAssignment
        fields = ("id", "user", "scope_node", "assignment_type", "created_at")
        read_only_fields = ("id", "created_at")


class UserRoleAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserRoleAssignment
        fields = ("id", "user", "role", "scope_node", "created_at")
        read_only_fields = ("id", "created_at")
