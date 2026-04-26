from apps.access.models import (
    Role,
    Permission,
    RolePermission,
    UserScopeAssignment,
    UserRoleAssignment,
)
from apps.core.services import get_ancestors


def assign_user_to_scope(user, scope_node, assignment_type):
    """Create or retrieve a UserScopeAssignment."""
    obj, _ = UserScopeAssignment.objects.get_or_create(
        user=user,
        scope_node=scope_node,
        assignment_type=assignment_type,
    )
    return obj


def assign_user_role(user, role, scope_node):
    """Create or retrieve a UserRoleAssignment."""
    obj, _ = UserRoleAssignment.objects.get_or_create(
        user=user,
        role=role,
        scope_node=scope_node,
    )
    return obj


def grant_permission_to_role(role, permission):
    """Attach a Permission to a Role."""
    obj, _ = RolePermission.objects.get_or_create(role=role, permission=permission)
    return obj


def user_has_permission_at_node(user, action, resource, scope_node):
    """
    Check if user holds a role granting (action, resource) at exactly scope_node.
    No upward inheritance — every assignment must be explicit.
    """
    return UserRoleAssignment.objects.filter(
        user=user,
        scope_node=scope_node,
        role__role_permissions__permission__action=action,
        role__role_permissions__permission__resource=resource,
        role__is_active=True,
    ).exists()


def user_can_act_on_scope(user, scope_node_id: int) -> bool:
    """
    Returns True if the user has an actionable (direct) assignment at scope_node_id.

    Unlike visible scopes which include descendants, actionable scope requires
    an explicit direct assignment at exactly that node.
    """
    from apps.access.selectors import get_user_actionable_scope_ids
    actionable_ids = get_user_actionable_scope_ids(user)
    # Normalise to int since request data may pass string IDs
    return int(scope_node_id) in [int(x) for x in actionable_ids]


def user_can_act_on_scope_or_ancestors(user, scope_node_id: int) -> bool:
    """
    Returns True if the user has an actionable (direct) assignment at the given
    scope node OR any of its ancestors.

    Use this when a module intentionally allows parent-scope operators to
    manage child-scope records without relaxing the global actionable-scope
    semantics for every feature.
    """
    from apps.core.models import ScopeNode
    from apps.access.selectors import get_user_actionable_scope_ids

    try:
        node = ScopeNode.objects.select_related("org").get(pk=scope_node_id)
    except ScopeNode.DoesNotExist:
        return False

    actionable_ids = {int(x) for x in get_user_actionable_scope_ids(user)}
    if int(scope_node_id) in actionable_ids:
        return True

    return any(int(ancestor.id) in actionable_ids for ancestor in get_ancestors(node))


def user_can_act_on_scope_response(user, scope_node_id: int, action: str = "this action"):
    """
    Returns a 403 Response if user cannot act on the given scope_node_id.
    Returns None if the user is allowed to proceed.

    Use for mutation/action endpoints that need explicit scope enforcement.
    """
    from rest_framework.response import Response
    from rest_framework import status
    if not user_can_act_on_scope(user, scope_node_id):
        return Response(
            {"detail": f"You do not have permission to {action} at this scope."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def user_can_act_on_scope_or_ancestors_response(user, scope_node_id: int, action: str = "this action"):
    """
    Returns a 403 Response if user cannot act on the given scope node or any of
    its ancestors. Returns None if the user is allowed to proceed.
    """
    from rest_framework.response import Response
    from rest_framework import status

    if not user_can_act_on_scope_or_ancestors(user, scope_node_id):
        return Response(
            {"detail": f"You do not have permission to {action} at this scope."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def user_has_permission_including_ancestors(user, action, resource, scope_node):
    """
    Walk-up permission check: returns True if the user has (action, resource)
    at scope_node OR any of its ancestors.
    Uses the materialized path for O(1) ancestor path extraction.
    """
    if user_has_permission_at_node(user, action, resource, scope_node):
        return True
    for ancestor in get_ancestors(scope_node):
        if user_has_permission_at_node(user, action, resource, ancestor):
            return True
    return False
