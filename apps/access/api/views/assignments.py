from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.access.models import UserScopeAssignment, UserRoleAssignment
from apps.access.api.serializers.assignments import (
    UserScopeAssignmentSerializer,
    UserRoleAssignmentSerializer,
)
from apps.access.services import user_can_act_on_scope_response


class UserScopeAssignmentViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = UserScopeAssignmentSerializer

    def get_queryset(self):
        qs = UserScopeAssignment.objects.select_related("user", "scope_node")
        user_id = self.request.query_params.get("user")
        node_id = self.request.query_params.get("scope_node")
        if user_id:
            qs = qs.filter(user_id=user_id)
        if node_id:
            qs = qs.filter(scope_node_id=node_id)
        return qs

    def create(self, request, *args, **kwargs):
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_response(request.user, scope_node_id, "assign a user to this scope"):
                return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        assignment = self.get_object()
        if err := user_can_act_on_scope_response(request.user, assignment.scope_node_id, "update this scope assignment"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        assignment = self.get_object()
        if err := user_can_act_on_scope_response(request.user, assignment.scope_node_id, "update this scope assignment"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        assignment = self.get_object()
        if err := user_can_act_on_scope_response(request.user, assignment.scope_node_id, "remove this scope assignment"):
            return err
        return super().destroy(request, *args, **kwargs)


class UserRoleAssignmentViewSet(ModelViewSet):
    """
    Authority: mutations require the caller to have a direct role assignment
    at the target scope_node (actionable scope). This prevents any authenticated
    user from granting themselves or others elevated roles.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = UserRoleAssignmentSerializer

    def get_queryset(self):
        qs = UserRoleAssignment.objects.select_related("user", "role", "scope_node")
        user_id = self.request.query_params.get("user")
        node_id = self.request.query_params.get("scope_node")
        role_id = self.request.query_params.get("role")
        if user_id:
            qs = qs.filter(user_id=user_id)
        if node_id:
            qs = qs.filter(scope_node_id=node_id)
        if role_id:
            qs = qs.filter(role_id=role_id)
        return qs

    def create(self, request, *args, **kwargs):
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_response(request.user, scope_node_id, "assign a role at this scope"):
                return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        assignment = self.get_object()
        if err := user_can_act_on_scope_response(request.user, assignment.scope_node_id, "update this role assignment"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        assignment = self.get_object()
        if err := user_can_act_on_scope_response(request.user, assignment.scope_node_id, "update this role assignment"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        assignment = self.get_object()
        if err := user_can_act_on_scope_response(request.user, assignment.scope_node_id, "remove this role assignment"):
            return err
        return super().destroy(request, *args, **kwargs)
