from rest_framework import serializers
from apps.modules.models import ModuleActivation
from apps.modules.services import resolve_module_activation


class ModuleActivationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModuleActivation
        fields = (
            "id", "module", "scope_node", "is_active",
            "override_parent", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class ModuleActivationResolveSerializer(serializers.Serializer):
    """Read-only serializer to resolve effective activation for a node."""
    module = serializers.CharField()
    scope_node = serializers.IntegerField()
    is_active = serializers.SerializerMethodField()

    def get_is_active(self, obj):
        from apps.core.models import ScopeNode
        try:
            node = ScopeNode.objects.get(pk=obj["scope_node"])
        except ScopeNode.DoesNotExist:
            return False
        return resolve_module_activation(obj["module"], node)
