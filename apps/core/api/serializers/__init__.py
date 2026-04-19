from rest_framework import serializers
from apps.core.models import Organization, ScopeNode


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ("id", "name", "code", "is_active", "created_at")
        read_only_fields = ("id", "created_at")


class ScopeNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScopeNode
        fields = (
            "id", "org", "parent", "name", "code", "node_type",
            "path", "depth", "is_active", "created_at",
        )
        read_only_fields = ("id", "path", "depth", "created_at")


class ScopeNodeTreeSerializer(serializers.ModelSerializer):
    """Recursive serializer for tree representation."""
    children = serializers.SerializerMethodField()

    class Meta:
        model = ScopeNode
        fields = (
            "id", "name", "code", "node_type", "path", "depth",
            "is_active", "children",
        )

    def get_children(self, obj):
        qs = obj.children.filter(is_active=True).order_by("name")
        return ScopeNodeTreeSerializer(qs, many=True).data
