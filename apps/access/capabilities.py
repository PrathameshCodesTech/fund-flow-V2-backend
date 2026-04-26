"""
Central capability mapping.

Role codes (from Role.code) are mapped to UI-facing capability strings.
The mapping is deterministic and easy to extend when new roles are added.

Design:
- tenant_admin: all internal capabilities (system-wide)
- org_admin:    all internal capabilities (org-wide)
- Other roles:  scoped to their seeded permissions + workflow actions

Static mapping (role code -> set of capability strings) keeps this
fast and introspectable. If a role's capabilities need to vary by scope
node, the caller can filter further using the user's actual
UserRoleAssignment.scope_node values.
"""

from __future__ import annotations

from typing import FrozenSet

# ── Capability constants ────────────────────────────────────────────────────────

ALL_CAPABILITIES: FrozenSet[str] = frozenset([
    # Budget
    "budget.view",
    "budget.manage",
    # Invoice
    "invoice.view",
    "invoice.create",
    "invoice.edit_draft",
    "invoice.submit",
    "invoice.comment",
    "invoice.review",
    "invoice.approve",
    "invoice.manage",
    # Campaign
    "campaign.view",
    "campaign.create",
    "campaign.edit",
    "campaign.manage",
    # Vendor
    "vendor.view",
    "vendor.create",
    "vendor.manage",
    # Workflow
    "workflow.task.view",
    "workflow.step.approve",
    "workflow.step.reject",
    "workflow.manage",
    # Reporting
    "reporting.view_basic",
    "reporting.view_region",
    "reporting.view_finance",
    "reporting.view_all",
    # Org / IAM
    "organization.manage",
    "iam.manage",
    # Internal portal
    "portal.vendor",
])

# ── Role → capabilities mapping ────────────────────────────────────────────────

_ROLE_CAPABILITIES: dict[str, FrozenSet[str]] = {
    # System-wide admin: everything except vendor portal (that's for vendor users)
    "tenant_admin": frozenset([
        "budget.view",
        "budget.manage",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "invoice.manage",
        "campaign.view",
        "campaign.create",
        "campaign.edit",
        "campaign.manage",
        "vendor.view",
        "vendor.create",
        "vendor.manage",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "workflow.manage",
        "reporting.view_basic",
        "reporting.view_region",
        "reporting.view_finance",
        "reporting.view_all",
        "organization.manage",
        "iam.manage",
    ]),

    # Org admin: same as tenant_admin
    "org_admin": frozenset([
        "budget.view",
        "budget.manage",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "invoice.manage",
        "campaign.view",
        "campaign.create",
        "campaign.edit",
        "campaign.manage",
        "vendor.view",
        "vendor.create",
        "vendor.manage",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "workflow.manage",
        "reporting.view_basic",
        "reporting.view_region",
        "reporting.view_finance",
        "reporting.view_all",
        "organization.manage",
        "iam.manage",
    ]),

    # Marketing Executive: creates/submits invoices, creates campaigns, views budgets.
    # Receives workflow tasks for their submitted invoices.
    "marketing_executive": frozenset([
        "budget.view",
        "invoice.view",
        "invoice.create",
        "invoice.submit",
        "campaign.view",
        "campaign.create",
        "vendor.view",
        "workflow.task.view",
    ]),

    # Marketing Head: approve + manage on top of view/create.
    # Reviews submitted invoices and manages campaigns at regional level.
    "marketing_head": frozenset([
        "budget.view",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "campaign.view",
        "campaign.create",
        "campaign.edit",
        "campaign.manage",
        "vendor.view",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "reporting.view_basic",
        "reporting.view_region",
    ]),

    # Marketing Manager: current Hiparks role code, same capability intent
    # as the legacy marketing_head role.
    "marketing_manager": frozenset([
        "budget.view",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "campaign.view",
        "campaign.create",
        "campaign.edit",
        "campaign.manage",
        "vendor.view",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "reporting.view_basic",
        "reporting.view_region",
    ]),

    # HO Executive: approves invoices at company level (HO = Head Office).
    # Same capability set as HO Head.
    "ho_executive": frozenset([
        "budget.view",
        "budget.manage",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "invoice.manage",
        "campaign.view",
        "campaign.create",
        "campaign.edit",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "reporting.view_basic",
        "reporting.view_region",
        "reporting.view_finance",
    ]),

    # HO Head: same as HO Executive (approves at company level)
    "ho_head": frozenset([
        "budget.view",
        "budget.manage",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "invoice.manage",
        "campaign.view",
        "campaign.create",
        "campaign.edit",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "reporting.view_basic",
        "reporting.view_region",
        "reporting.view_finance",
    ]),

    # HOD: current Hiparks role code, same capability intent
    # as the legacy ho_head role.
    "hod": frozenset([
        "budget.view",
        "budget.manage",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "invoice.manage",
        "campaign.view",
        "campaign.create",
        "campaign.edit",
        "vendor.view",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "reporting.view_basic",
        "reporting.view_region",
        "reporting.view_finance",
    ]),

    # Finance Team: full invoice cycle, finance reporting, vendor view for invoice matching.
    # Does NOT get campaign.manage or vendor.manage (those are admin functions).
    "finance_team": frozenset([
        "budget.view",
        "budget.manage",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "invoice.manage",
        "campaign.view",
        "vendor.view",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "workflow.manage",
        "reporting.view_basic",
        "reporting.view_region",
        "reporting.view_finance",
    ]),

    # Entity Manager: invoice management at entity level, no budget manage.
    "entity_manager": frozenset([
        "budget.view",
        "invoice.view",
        "invoice.create",
        "invoice.edit_draft",
        "invoice.submit",
        "invoice.comment",
        "invoice.review",
        "invoice.approve",
        "campaign.view",
        "campaign.create",
        "workflow.task.view",
        "workflow.step.approve",
        "workflow.step.reject",
        "reporting.view_basic",
        "reporting.view_region",
    ]),
}


def get_capabilities_for_role(role_code: str) -> FrozenSet[str]:
    """Return the capability set for a single role code."""
    return _ROLE_CAPABILITIES.get(role_code, frozenset())


def get_user_capabilities(user) -> list[str]:
    """
    Compute all UI-facing capabilities for a user.

    - tenant_admin / superuser: all internal capabilities
    - Other users: union of capabilities from all their active role assignments

    Vendor portal users additionally receive the "portal.vendor" capability.
    """
    # System-wide admin always gets everything
    if user.is_superuser or _has_role(user, "tenant_admin"):
        caps = set(ALL_CAPABILITIES - {"portal.vendor"})
    else:
        caps = set()
        for assignment in user.role_assignments.filter(role__is_active=True).select_related("role"):
            caps |= get_capabilities_for_role(assignment.role.code)

    # Vendor portal users get their portal capability
    if _is_vendor_portal_user(user):
        caps.add("portal.vendor")

    return sorted(caps)


def _has_role(user, role_code: str) -> bool:
    from apps.access.models import UserRoleAssignment
    return UserRoleAssignment.objects.filter(
        user=user,
        role__code=role_code,
        role__is_active=True,
    ).exists()


def _is_vendor_portal_user(user) -> bool:
    from apps.vendors.models import UserVendorAssignment
    return UserVendorAssignment.objects.filter(user=user, is_active=True).exists()
