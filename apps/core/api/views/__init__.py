from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.core.models import Organization, ScopeNode
from apps.core.api.serializers import (
    OrganizationSerializer,
    ScopeNodeSerializer,
    ScopeNodeTreeSerializer,
)
from apps.core.services import (
    build_node_path,
    get_node_depth,
    update_descendant_paths,
    get_subtree_nodes,
    get_ancestors,
)


class OrganizationViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = OrganizationSerializer
    queryset = Organization.objects.all().order_by("name")


class ScopeNodeViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ScopeNodeSerializer

    def get_queryset(self):
        qs = ScopeNode.objects.select_related("org", "parent").order_by("depth", "name")
        org_id = self.request.query_params.get("org")
        if org_id:
            qs = qs.filter(org_id=org_id)
        return qs

    def perform_create(self, serializer):
        parent = serializer.validated_data.get("parent")
        org = serializer.validated_data["org"]
        code = serializer.validated_data["code"]
        path = build_node_path(parent, org, code)
        depth = get_node_depth(parent)
        serializer.save(path=path, depth=depth)

    def perform_update(self, serializer):
        old_path = serializer.instance.path
        instance = serializer.save()
        # Recompute path/depth if parent or code changed
        parent = instance.parent
        org = instance.org
        new_path = build_node_path(parent, org, instance.code)
        new_depth = get_node_depth(parent)
        if new_path != old_path:
            instance.path = new_path
            instance.depth = new_depth
            instance.save(update_fields=["path", "depth"])
            update_descendant_paths(instance, old_path)

    @action(detail=True, methods=["get"])
    def tree(self, request, pk=None):
        """Return the subtree rooted at this node."""
        node = self.get_object()
        serializer = ScopeNodeTreeSerializer(node)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def ancestors(self, request, pk=None):
        """Return all ancestors ordered root-first."""
        node = self.get_object()
        ancestors = get_ancestors(node)
        serializer = ScopeNodeSerializer(ancestors, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def subtree(self, request, pk=None):
        """Return all nodes in the subtree (node + descendants), ordered by depth."""
        node = self.get_object()
        nodes = get_subtree_nodes(node)
        serializer = ScopeNodeSerializer(nodes, many=True)
        return Response(serializer.data)
