from decimal import Decimal

from django.db import transaction

from apps.campaigns.models import Campaign, CampaignStatus
from apps.budgets.models import SourceType
from apps.budgets.services import (
    reserve_budget,
    release_reserved_budget,
    review_variance_request,
    get_source_reserved_balance,
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
        draft → budget_variance_pending  (budget linked, variance required)

    Returns dict with:
        status: "no_budget_linked" | "reserved" | "reserved_with_warning" | "variance_required"
        + reserve_budget() result keys when budget is linked

    Raises:
        CampaignStateError  — if campaign is not DRAFT
        BudgetLimitExceeded — if reservation would exceed hard block threshold
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

    result = reserve_budget(
        budget=campaign.budget,
        amount=campaign.requested_amount,
        source_type=SourceType.CAMPAIGN,
        source_id=str(campaign.id),
        requested_by=submitted_by,
        note=f"Reserve for campaign {campaign.code}",
    )

    if result["status"] in ("reserved", "reserved_with_warning"):
        campaign.status = CampaignStatus.PENDING_WORKFLOW
        campaign.save(update_fields=["status", "updated_at"])
    elif result["status"] == "variance_required":
        campaign.budget_variance_request = result["variance_request"]
        campaign.status = CampaignStatus.BUDGET_VARIANCE_PENDING
        campaign.save(update_fields=["status", "budget_variance_request", "updated_at"])

    return result


# ---------------------------------------------------------------------------
# Review campaign budget variance
# ---------------------------------------------------------------------------

@transaction.atomic
def review_campaign_budget_variance(
    campaign: Campaign,
    decision: str,
    reviewed_by,
    review_note: str = "",
):
    """
    Approve or reject the budget variance request attached to the campaign.

    Transitions:
        budget_variance_pending → pending_workflow  (if approved)
        budget_variance_pending → rejected          (if rejected)

    Returns the updated BudgetVarianceRequest.

    Raises:
        CampaignStateError — if campaign is not BUDGET_VARIANCE_PENDING
        ValueError         — if campaign has no attached variance request,
                             or decision is invalid
    """
    if campaign.status != CampaignStatus.BUDGET_VARIANCE_PENDING:
        raise CampaignStateError(
            f"Campaign {campaign.id} is {campaign.status!r}, expected 'budget_variance_pending'."
        )
    if not campaign.budget_variance_request_id:
        raise ValueError(
            f"Campaign {campaign.id} has no budget_variance_request attached."
        )

    updated_variance = review_variance_request(
        variance_request=campaign.budget_variance_request,
        decision=decision,
        reviewed_by=reviewed_by,
        review_note=review_note,
    )

    if decision == "approved":
        campaign.status = CampaignStatus.PENDING_WORKFLOW
    else:
        campaign.status = CampaignStatus.REJECTED

    campaign.save(update_fields=["status", "updated_at"])
    return updated_variance


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
                note=note or f"Campaign {campaign.code} cancelled",
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
