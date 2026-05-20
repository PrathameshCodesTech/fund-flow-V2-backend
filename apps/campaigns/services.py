from decimal import Decimal

from django.db import transaction

from apps.campaigns.models import Campaign, CampaignStatus
from apps.budgets.models import SourceType
from apps.budgets.services import (
    release_reserved_budget_line,
    get_source_reserved_balance,
    reserve_budget_line,
    resolve_budget_line_for_allocation,
    BudgetLineNotFoundError,
    BudgetLimitExceeded,
    BudgetNotActiveError,
)

# Re-export workflow errors so callers don't need to know where they live
from apps.workflow.services import (  # noqa: F401
    ModuleInactiveError,
    WorkflowNotConfiguredError,
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CampaignStateError(ValueError):
    """Raised when a campaign is in the wrong state for the requested operation."""


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@transaction.atomic
def create_campaign(
    scope_node,
    name: str,
    code: str,
    requested_amount: Decimal,
    created_by,
    org=None,
    **kwargs,
) -> Campaign:
    """Create a new campaign in DRAFT status."""
    campaign = Campaign.objects.create(
        org=org,
        scope_node=scope_node,
        name=name,
        code=code,
        requested_amount=requested_amount,
        created_by=created_by,
        status=CampaignStatus.DRAFT,
        **kwargs,
    )
    return campaign


# ---------------------------------------------------------------------------
# Submit for budget
# ---------------------------------------------------------------------------

@transaction.atomic
def submit_campaign_for_budget(campaign: Campaign, submitted_by) -> dict:
    """
    Attempt budget reservation for the campaign.

    Transitions:
        draft → pending_workflow   (no budget linked, or reservation succeeded)

    Returns dict with:
        status: "no_budget_linked" | "reserved"
        + reserve_budget_line() result keys when budget is linked

    Raises:
        CampaignStateError  — if campaign is not DRAFT
        BudgetLimitExceeded — if reservation would exceed available balance
        BudgetNotActiveError — if linked budget is not ACTIVE
    """
    if campaign.status != CampaignStatus.DRAFT:
        raise CampaignStateError(
            f"Campaign {campaign.id} is {campaign.status!r}, expected 'draft'."
        )

    if not campaign.budget_id:
        campaign.status = CampaignStatus.PENDING_WORKFLOW
        campaign.save(update_fields=["status", "updated_at"])
        return {"status": "no_budget_linked"}

    # Resolve the BudgetLine for this campaign's category/subcategory context
    try:
        budget_line = resolve_budget_line_for_allocation(
            budget=campaign.budget,
            category_id=campaign.category_id,
            subcategory_id=campaign.subcategory_id,
        )
    except BudgetLineNotFoundError:
        raise BudgetLineNotFoundError(
            f"No BudgetLine found for budget={campaign.budget_id} "
            f"matching campaign's category={campaign.category_id}, "
            f"subcategory={campaign.subcategory_id}. "
            "A budget line must exist before a campaign can reserve against it."
        )

    result = reserve_budget_line(
        line=budget_line,
        amount=campaign.requested_amount,
        source_type=SourceType.CAMPAIGN,
        source_id=str(campaign.id),
        requested_by=submitted_by,
        note=f"Reserve for campaign {campaign.code}",
    )

    if result["status"] == "reserved":
        campaign.status = CampaignStatus.PENDING_WORKFLOW
        campaign.save(update_fields=["status", "updated_at"])
    else:
        raise ValueError(
            f"Unexpected budget reservation result for campaign {campaign.id}: {result['status']!r}"
        )

    return result


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

@transaction.atomic
def cancel_campaign(campaign: Campaign, cancelled_by, note: str = "") -> Campaign:
    """
    Cancel a campaign in any pre-completion state.

    Allowed from: draft, pending_budget, budget_variance_pending,
                  pending_workflow, in_review.

    If the campaign has an active budget reservation, it is released first.

    Raises:
        CampaignStateError — if campaign is in a terminal or non-cancellable status
    """
    cancellable = {
        CampaignStatus.DRAFT,
        CampaignStatus.PENDING_BUDGET,
        CampaignStatus.BUDGET_VARIANCE_PENDING,
        CampaignStatus.PENDING_WORKFLOW,
        CampaignStatus.IN_REVIEW,
    }
    if campaign.status not in cancellable:
        raise CampaignStateError(
            f"Campaign {campaign.id} in status {campaign.status!r} cannot be cancelled."
        )

    # Release the net reserved balance for this campaign source
    if campaign.budget_id:
        campaign.budget.refresh_from_db()
        # Try line-level release first (new campaigns); fall back to header for legacy
        try:
            budget_line = resolve_budget_line_for_allocation(
                budget=campaign.budget,
                category_id=campaign.category_id,
                subcategory_id=campaign.subcategory_id,
            )
            from apps.budgets.services import get_source_reserved_balance_for_line
            net_balance = get_source_reserved_balance_for_line(
                budget_line,
                source_type=SourceType.CAMPAIGN,
                source_id=str(campaign.id),
            )
            if net_balance > Decimal("0"):
                release_reserved_budget_line(
                    line=budget_line,
                    amount=net_balance,
                    source_type=SourceType.CAMPAIGN,
                    source_id=str(campaign.id),
                    released_by=cancelled_by,
                    note=note or f"Campaign {campaign.code} cancelled",
                )
        except BudgetLineNotFoundError:
            # Legacy campaign without matching BudgetLine — fall back to header release
            net_balance = get_source_reserved_balance(
                budget=campaign.budget,
                source_type=SourceType.CAMPAIGN,
                source_id=str(campaign.id),
            )
            if net_balance > Decimal("0"):
                release_reserved_budget(
                    budget=campaign.budget,
                    amount=net_balance,
                    source_type=SourceType.CAMPAIGN,
                    source_id=str(campaign.id),
                    released_by=cancelled_by,
                    note=note or f"Campaign {campaign.code} cancelled (legacy)",
                )

    campaign.status = CampaignStatus.CANCELLED
    campaign.save(update_fields=["status", "updated_at"])
    return campaign


# ---------------------------------------------------------------------------
# Workflow draft creation
# ---------------------------------------------------------------------------

def create_campaign_workflow_draft(campaign: Campaign, started_by, assignments=None, activate: bool = False):
    """
    Create a workflow instance draft for a campaign.

    Thin orchestration layer — delegates business logic to workflow services.
    Campaign must be in PENDING_WORKFLOW status.

    Args:
        campaign:    Campaign instance
        started_by:  User who is starting the workflow
        assignments: Optional dict { str(step_id): user_id } for pre-assignment overrides
        activate:    If True, also activate the draft immediately

    Returns:
        WorkflowInstance (DRAFT or ACTIVE depending on `activate`)

    Raises:
        CampaignStateError         — if campaign is not PENDING_WORKFLOW
        ModuleInactiveError        — if campaign module is inactive at the scope node
        WorkflowNotConfiguredError — if no published template found in walk-up chain
        ValueError                 — if assignment overrides are invalid or activation fails
    """
    if campaign.status != CampaignStatus.PENDING_WORKFLOW:
        raise CampaignStateError(
            f"Campaign {campaign.id} is {campaign.status!r}, expected 'pending_workflow'."
        )

    from apps.workflow.services import (
        resolve_workflow_template_version,
        create_workflow_instance_draft,
        apply_step_assignment_overrides,
        activate_workflow_instance,
    )

    template_version = resolve_workflow_template_version(
        module="campaign",
        scope_node=campaign.scope_node,
    )

    instance = create_workflow_instance_draft(
        template_version=template_version,
        subject_type="campaign",
        subject_id=campaign.pk,
        subject_scope_node=campaign.scope_node,
        started_by=started_by,
    )

    if assignments:
        apply_step_assignment_overrides(instance, assignments, campaign.scope_node)

    if activate:
        instance = activate_workflow_instance(instance, activated_by=started_by)

    return instance
