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

    def validate(self, attrs):
        role = attrs.get("role", getattr(self.instance, "role", None))
        scope_node = attrs.get("scope_node", getattr(self.instance, "scope_node", None))
        if role and not role.is_active:
            raise serializers.ValidationError({"role": "The selected role is inactive."})
        if scope_node and not scope_node.is_active:
            raise serializers.ValidationError({"scope_node": "The selected scope is inactive."})
        if role and scope_node and role.org_id != scope_node.org_id:
            raise serializers.ValidationError({
                "role": "The selected role belongs to a different organization than the selected scope.",
            })
        if role and scope_node and role.node_type_scope and role.node_type_scope != scope_node.node_type:
            raise serializers.ValidationError({
                "scope_node": (
                    f"The {role.name} role can only be assigned at {role.node_type_scope} scope nodes."
                ),
            })
        return attrs
