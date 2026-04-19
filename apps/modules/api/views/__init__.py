from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from apps.modules.models import ModuleActivation
from apps.modules.services import resolve_module_activation
from apps.modules.api.serializers import ModuleActivationSerializer
from apps.core.models import ScopeNode
from apps.access.selectors import get_user_visible_scope_ids
from apps.access.services import user_can_act_on_scope_response


class ModuleActivationViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ModuleActivationSerializer

    def get_queryset(self):
        # Visibility = subtree: user sees activations for scope nodes in their visible set.
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = ModuleActivation.objects.select_related("scope_node").filter(
            scope_node_id__in=visible_scope_ids
        ).order_by("id")
        node_id = self.request.query_params.get("scope_node")
        module = self.request.query_params.get("module")
        if node_id:
            qs = qs.filter(scope_node_id=node_id)
        if module:
            qs = qs.filter(module=module)
        return qs

    def create(self, request, *args, **kwargs):
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_response(request.user, scope_node_id, "activate a module"):
                return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        activation = self.get_object()
        if err := user_can_act_on_scope_response(request.user, activation.scope_node_id, "update this module activation"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        activation = self.get_object()
        if err := user_can_act_on_scope_response(request.user, activation.scope_node_id, "update this module activation"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        activation = self.get_object()
        if err := user_can_act_on_scope_response(request.user, activation.scope_node_id, "delete this module activation"):
            return err
        return super().destroy(request, *args, **kwargs)


class ModuleActivationResolveView(APIView):
    """
    GET /api/v1/modules/resolve/?module=invoice&scope_node=42
    Returns effective is_active after walk-up resolution.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        module = request.query_params.get("module")
        node_id = request.query_params.get("scope_node")
        if not module or not node_id:
            return Response(
                {"detail": "Both 'module' and 'scope_node' query params are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            node = ScopeNode.objects.get(pk=node_id)
        except ScopeNode.DoesNotExist:
            return Response({"detail": "ScopeNode not found."}, status=status.HTTP_404_NOT_FOUND)

        is_active = resolve_module_activation(module, node)
        return Response({"module": module, "scope_node": node_id, "is_active": is_active})
