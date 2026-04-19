from apps.invoices.models import Invoice
from apps.core.services import get_ancestors
from apps.access.models import PermissionAction, PermissionResource


def get_invoices_for_node(scope_node):
    return Invoice.objects.filter(scope_node=scope_node).select_related(
        "scope_node", "created_by"
    ).order_by("-created_at")


def get_invoices_created_by(user):
    return Invoice.objects.filter(created_by=user).select_related(
        "scope_node"
    ).order_by("-created_at")


def get_invoice_by_id(invoice_id):
    return Invoice.objects.select_related("scope_node", "created_by").get(pk=invoice_id)


def user_can_access_invoice(user, invoice):
    """
    Authorization rule for invoice read/update.

    User may access an invoice if ANY of:
      1. They are the invoice creator
      2. They have READ permission on INVOICE at invoice.scope_node or any ancestor
    """
    if invoice.created_by_id == user.pk:
        return True
    from apps.access.services import user_has_permission_including_ancestors
    return user_has_permission_including_ancestors(
        user, PermissionAction.READ, PermissionResource.INVOICE, invoice.scope_node
    )


def user_can_update_invoice(user, invoice):
    """
    Authorization rule for invoice update.

    User may update an invoice if ANY of:
      1. They are the invoice creator
      2. They have UPDATE permission on INVOICE at invoice.scope_node or any ancestor

    NOTE: Currently the Invoice model does not support update through the API
    (no update serializer defined). This selector is provided for future use
    and consistency with the authorization model.
    """
    if invoice.created_by_id == user.pk:
        return True
    from apps.access.services import user_has_permission_including_ancestors
    return user_has_permission_including_ancestors(
        user, PermissionAction.UPDATE, PermissionResource.INVOICE, invoice.scope_node
    )


def filter_invoices_readable_for_user(user, qs):
    """
    Filter a queryset of Invoices to only those the user can read.
    Combines creator-match and permission-at-node-or-ancestor checks.
    """
    from django.db.models import Q, Exists, OuterRef
    from apps.access.models import UserScopeAssignment, UserRoleAssignment, RolePermission, Permission
    from apps.core.models import ScopeNode

    creator_filter = Q(created_by=user)

    # Build ancestor path list for each invoice — done in Python for clarity.
    # This is acceptable because the result set is already filtered by other
    # query params before this call. For large lists, this could be pushed to
    # a raw SQL / CTE, but Python filtering is correct for this phase.
    readable_ids = []
    for invoice in qs:
        if user_can_access_invoice(user, invoice):
            readable_ids.append(invoice.pk)

    return qs.filter(pk__in=readable_ids)
