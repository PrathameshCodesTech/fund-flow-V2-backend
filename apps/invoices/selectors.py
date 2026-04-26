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


def user_can_begin_invoice_review(user, invoice, template_version):
    """
    Return True if user is allowed to begin review for this invoice+route.

    Authorization rule (permission-based):
      1. User has START_WORKFLOW permission on INVOICE at invoice's scope node
         or any ancestor.  This covers org_admin (which has all permissions
         via its role grant), tenant_admin, and any future role granted
         START_WORKFLOW:INVOICE — no role names are hardcoded.
      OR
      2. User is eligible for the first actionable human step of
         template_version resolved at the invoice's scope node.

    Roles are configurable bundles.  Permissions are the stable enterprise
    authorization contract.  Grant a role START_WORKFLOW:INVOICE to allow it
    to claim pending invoices; do not hardcode role names.
    """
    from apps.access.services import user_has_permission_including_ancestors
    from apps.workflow.services import get_first_actionable_step, get_eligible_users_for_step

    if user_has_permission_including_ancestors(
        user,
        PermissionAction.START_WORKFLOW,
        PermissionResource.INVOICE,
        invoice.scope_node,
    ):
        return True

    first_step = get_first_actionable_step(template_version)
    if not first_step:
        return False
    return get_eligible_users_for_step(first_step, invoice.scope_node).filter(pk=user.pk).exists()


def get_invoice_eligible_workflow_routes(invoice, user=None):
    """
    Return all active published workflow routes for an invoice (walks scope ancestors).

    Each route dict includes:
        template_id, template_name, template_code, version_id, version_number,
        first_step_name, user_can_begin (None when user not supplied).

    Mirrors the logic of eligible_workflows but adds first-step metadata and
    per-user begin eligibility.
    """
    from apps.workflow.models import WorkflowTemplate, WorkflowTemplateVersion, VersionStatus
    from apps.core.services import get_ancestors
    from apps.workflow.services import get_first_actionable_step

    nodes_to_check = [invoice.scope_node] + list(get_ancestors(invoice.scope_node).order_by("-depth"))
    version_ids_seen = set()
    routes = []

    for node in nodes_to_check:
        templates = WorkflowTemplate.objects.filter(module="invoice", scope_node=node, is_active=True)
        for template in templates:
            published = (
                WorkflowTemplateVersion.objects
                .filter(template=template, status=VersionStatus.PUBLISHED)
                .order_by("-version_number")
                .first()
            )
            if not published or published.id in version_ids_seen:
                continue
            version_ids_seen.add(published.id)

            first_step = get_first_actionable_step(published)
            can_begin = (
                user_can_begin_invoice_review(user, invoice, published) if user is not None else None
            )
            routes.append({
                "template_id": template.id,
                "template_name": template.name,
                "template_code": template.code,
                "version_id": published.id,
                "version_number": published.version_number,
                "first_step_name": first_step.name if first_step else None,
                "user_can_begin": can_begin,
            })

    return routes


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


def user_can_record_invoice_payment(user, invoice):
    """Alias for service-level check — delegates to services."""
    from apps.invoices.services import can_user_record_invoice_payment
    return can_user_record_invoice_payment(user, invoice)
