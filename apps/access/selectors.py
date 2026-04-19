# ---------------------------------------------------------------------------
# Access Policy — Option 2 (Intentional Product Policy)
#
# VISIBILITY = SUBTREE (get_user_visible_scope_ids)
#   A user can read/list records in scopes where they have a direct
#   UserRoleAssignment AND in all descendant scopes below those assignments.
#   Use for: list, retrieve, detail endpoints.
#
# AUTHORITY = EXPLICIT ONLY (get_user_actionable_scope_ids / user_can_act_on_scope)
#   A user can mutate/approve/reassign/start/etc. ONLY where they have a
#   direct UserRoleAssignment at that exact node — no downward inheritance.
#   Use for: create, update, delete, state-transition action endpoints.
#
# INTENTIONAL EXCEPTIONS (ancestor walk-up allowed):
#   - workflow reassign/start: user_has_permission_including_ancestors is used
#     deliberately for START_WORKFLOW and REASSIGN permissions.
#   - module/template resolution: walk-up is for configuration resolution only,
#     not for granting generic mutation authority.
#
# This is the intended product rule, not an accidental loophole.
# ---------------------------------------------------------------------------

from apps.access.models import (
    Role,
    Permission,
    RolePermission,
    UserScopeAssignment,
    UserRoleAssignment,
)


def get_roles_for_org(org):
    return Role.objects.filter(org=org).order_by("name")


def get_active_roles_for_org(org):
    return Role.objects.filter(org=org, is_active=True).order_by("name")


def get_permissions():
    return Permission.objects.all().order_by("resource", "action")


def get_permissions_for_role(role):
    return Permission.objects.filter(role_permissions__role=role).order_by("resource", "action")


def get_scope_assignments_for_user(user):
    return UserScopeAssignment.objects.filter(user=user).select_related("scope_node")


def get_role_assignments_for_user(user):
    return UserRoleAssignment.objects.filter(user=user).select_related("role", "scope_node")


def get_role_assignments_at_node(scope_node):
    return UserRoleAssignment.objects.filter(scope_node=scope_node).select_related("user", "role")


def get_users_with_role_at_node(role, scope_node):
    """
    Returns queryset of ACTIVE users holding a specific role at a specific node.
    Inactive users are excluded so they never appear as eligible candidates
    or as auto-assigned default users.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.filter(
        role_assignments__role=role,
        role_assignments__scope_node=scope_node,
        is_active=True,
    )


def get_user_accessible_scope_ids(user) -> list:
    """
    Alias for get_user_visible_scope_ids for backward compatibility.
    Use get_user_visible_scope_ids or get_user_actionable_scope_ids explicitly.
    """
    return get_user_visible_scope_ids(user)


def get_user_accessible_org_ids(user) -> list:
    """
    Alias for get_user_visible_org_ids for backward compatibility.
    Use get_user_visible_org_ids or get_user_actionable_org_ids explicitly.
    """
    return get_user_visible_org_ids(user)


def get_user_direct_scope_ids(user) -> list:
    """
    Return the list of ScopeNode IDs where the user has a direct
    UserRoleAssignment — no descendant expansion.

    This is the set of nodes the user can directly act upon.
    Users with no role assignments receive an empty list.
    """
    return list(
        UserRoleAssignment.objects.filter(user=user)
        .values_list("scope_node_id", flat=True)
        .distinct()
    )


def get_user_visible_scope_ids(user) -> list:
    """
    Return the list of ScopeNode IDs visible to this user.

    Includes:
      - Nodes where the user has a direct UserRoleAssignment
      - All descendants of those nodes (via materialized-path prefix matching)

    Used for list/retrieve/detail read operations.
    Users with no role assignments receive an empty list — they see nothing.
    """
    from apps.core.models import ScopeNode
    from django.db.models import Q

    direct_paths = list(
        ScopeNode.objects.filter(
            user_role_assignments__user=user
        ).values_list("path", flat=True).distinct()
    )
    if not direct_paths:
        return []

    q = Q()
    for path in direct_paths:
        q |= Q(path__startswith=path)

    return list(ScopeNode.objects.filter(q).values_list("id", flat=True))


def get_user_actionable_scope_ids(user) -> list:
    """
    Return the list of ScopeNode IDs the user can directly act upon.

    Unlike visible scopes, this does NOT include descendants —
    authority requires an explicit assignment at the exact node.

    Used for all mutations: create, update, delete, and action endpoints.
    Users with no role assignments receive an empty list — they cannot act.
    """
    return list(
        UserRoleAssignment.objects.filter(user=user)
        .values_list("scope_node_id", flat=True)
        .distinct()
    )


def get_user_visible_org_ids(user) -> list:
    """
    Return the list of Organization IDs visible to the user,
    inferred from visible scope nodes.
    """
    from apps.core.models import ScopeNode

    ids = get_user_visible_scope_ids(user)
    if not ids:
        return []
    return list(
        ScopeNode.objects.filter(id__in=ids)
        .values_list("org_id", flat=True)
        .distinct()
    )


def get_user_actionable_org_ids(user) -> list:
    """
    Return the list of Organization IDs the user can act within,
    inferred from actionable/direct scope nodes.
    """
    from apps.core.models import ScopeNode

    ids = get_user_actionable_scope_ids(user)
    if not ids:
        return []
    return list(
        ScopeNode.objects.filter(id__in=ids)
        .values_list("org_id", flat=True)
        .distinct()
    )
