"""
Service-level tests for campaign runtime logic.
"""
import pytest
from decimal import Decimal

from apps.campaigns.models import Campaign, CampaignStatus
from apps.campaigns.services import (
    create_campaign,
    submit_campaign_for_budget,
    review_campaign_budget_variance,
    cancel_campaign,
    CampaignStateError,
)
from apps.budgets.models import (
    BudgetCategory, BudgetSubCategory, Budget, BudgetLine, BudgetRule,
    BudgetConsumption, BudgetVarianceRequest,
    BudgetStatus, PeriodType, ConsumptionType, ConsumptionStatus,
    VarianceStatus,
)
from apps.budgets.services import BudgetLimitExceeded
from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Campaign Org", code="campaign-org")


@pytest.fixture
def node(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/campaign-org/hq", depth=0, is_active=True,
    )


@pytest.fixture
def category(org):
    return BudgetCategory.objects.create(org=org, name="Marketing", code="marketing")


@pytest.fixture
def subcategory(category):
    return BudgetSubCategory.objects.create(category=category, name="Digital Ads", code="digital-ads")


@pytest.fixture
def user(db):
    return User.objects.create_user(email="camp@test.com", password="pass")


@pytest.fixture
def active_budget(org, node, category, subcategory, user):
    b = Budget.objects.create(
        org=org, scope_node=node,
        name="FY27 Marketing HQ",
        code="FY27-MKT-HQ",
        financial_year="2026-27", period_type=PeriodType.YEARLY,
        period_start="2026-04-01", period_end="2027-03-31",
        allocated_amount=Decimal("10000000.00"),
        reserved_amount=Decimal("0"), consumed_amount=Decimal("0"),
        currency="INR", status=BudgetStatus.ACTIVE, created_by=user,
    )
    BudgetLine.objects.create(
        budget=b,
        category=category,
        subcategory=subcategory,
        allocated_amount=Decimal("10000000.00"),
    )
    BudgetRule.objects.create(
        budget=b,
        warning_threshold_percent=Decimal("80.00"),
        approval_threshold_percent=Decimal("100.00"),
        hard_block_threshold_percent=Decimal("110.00"),
    )
    return b


def make_campaign(node, user, org=None, budget=None, amount=Decimal("500000.00"), **kwargs):
    return Campaign.objects.create(
        org=org,
        scope_node=node,
        name="Test Campaign",
        code="test-camp-001",
        requested_amount=amount,
        created_by=user,
        status=CampaignStatus.DRAFT,
        budget=budget,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. create_campaign creates draft
# ---------------------------------------------------------------------------

class TestCreateCampaign:
    def test_creates_in_draft_status(self, node, user, org):
        campaign = create_campaign(
            org=org,
            scope_node=node,
            name="My Campaign",
            code="my-camp-001",
            requested_amount=Decimal("100000.00"),
            created_by=user,
        )
        assert campaign.id is not None
        assert campaign.status == CampaignStatus.DRAFT
        assert campaign.created_by == user
        assert campaign.approved_amount == Decimal("0")


# ---------------------------------------------------------------------------
# 2. submit without budget → pending_workflow
# ---------------------------------------------------------------------------

class TestSubmitWithoutBudget:
    def test_no_budget_linked_goes_to_pending_workflow(self, node, user, org):
        campaign = make_campaign(node, user, org=org)
        result = submit_campaign_for_budget(campaign, submitted_by=user)
        assert result["status"] == "no_budget_linked"
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PENDING_WORKFLOW

    def test_submit_non_draft_raises(self, node, user, org):
        campaign = make_campaign(node, user, org=org)
        campaign.status = CampaignStatus.PENDING_WORKFLOW
        campaign.save()
        with pytest.raises(CampaignStateError):
            submit_campaign_for_budget(campaign, submitted_by=user)


# ---------------------------------------------------------------------------
# 3. submit with budget below warning → pending_workflow + reserved
# ---------------------------------------------------------------------------

class TestSubmitWithBudgetBelowWarning:
    def test_reserves_and_goes_to_pending_workflow(self, node, user, org, active_budget):
        # 500000 / 10000000 = 5% — well below 80% warning
        campaign = make_campaign(node, user, org=org, budget=active_budget, amount=Decimal("500000.00"))
        result = submit_campaign_for_budget(campaign, submitted_by=user)
        assert result["status"] == "reserved"
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PENDING_WORKFLOW
        active_budget.refresh_from_db()
        assert active_budget.reserved_amount == Decimal("500000.00")


# ---------------------------------------------------------------------------
# 4. submit with budget in warning band → pending_workflow + reserved_with_warning
# ---------------------------------------------------------------------------

class TestSubmitWithBudgetInWarningBand:
    def test_reserved_with_warning_goes_to_pending_workflow(self, node, user, org, active_budget):
        # 8500000 / 10000000 = 85% — above 80% warning, below 100% approval
        campaign = make_campaign(node, user, org=org, budget=active_budget, amount=Decimal("8500000.00"))
        result = submit_campaign_for_budget(campaign, submitted_by=user)
        assert result["status"] == "reserved_with_warning"
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PENDING_WORKFLOW
        active_budget.refresh_from_db()
        assert active_budget.reserved_amount == Decimal("8500000.00")


# ---------------------------------------------------------------------------
# 5. submit in variance band → budget_variance_pending + variance request attached
# ---------------------------------------------------------------------------

class TestSubmitWithBudgetInVarianceBand:
    def test_variance_required_goes_to_budget_variance_pending(self, node, user, org, active_budget):
        # 10500000 / 10000000 = 105% — above 100% approval, below 110% hard block
        campaign = make_campaign(node, user, org=org, budget=active_budget, amount=Decimal("10500000.00"))
        result = submit_campaign_for_budget(campaign, submitted_by=user)
        assert result["status"] == "variance_required"
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.BUDGET_VARIANCE_PENDING
        assert campaign.budget_variance_request is not None
        assert campaign.budget_variance_request.status == VarianceStatus.PENDING
        # Budget should NOT be reserved yet
        active_budget.refresh_from_db()
        assert active_budget.reserved_amount == Decimal("0")


# ---------------------------------------------------------------------------
# 6. submit above hard block → raises BudgetLimitExceeded, campaign stays draft
# ---------------------------------------------------------------------------

class TestSubmitAboveHardBlock:
    def test_hard_block_raises_and_campaign_stays_draft(self, node, user, org, active_budget):
        # 11500000 / 10000000 = 115% — above 110% hard block
        campaign = make_campaign(node, user, org=org, budget=active_budget, amount=Decimal("11500000.00"))
        with pytest.raises(BudgetLimitExceeded):
            submit_campaign_for_budget(campaign, submitted_by=user)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.DRAFT


# ---------------------------------------------------------------------------
# 7. approve campaign variance → pending_workflow
# ---------------------------------------------------------------------------

class TestApproveCampaignVariance:
    def test_approve_moves_to_pending_workflow(self, node, user, org, active_budget):
        campaign = make_campaign(node, user, org=org, budget=active_budget, amount=Decimal("10500000.00"))
        submit_campaign_for_budget(campaign, submitted_by=user)
        campaign.refresh_from_db()

        review_campaign_budget_variance(campaign, decision="approved", reviewed_by=user)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PENDING_WORKFLOW
        # Budget should now have the reservation applied
        active_budget.refresh_from_db()
        assert active_budget.reserved_amount == Decimal("10500000.00")

    def test_approve_wrong_status_raises(self, node, user, org):
        campaign = make_campaign(node, user, org=org)
        with pytest.raises(CampaignStateError):
            review_campaign_budget_variance(campaign, decision="approved", reviewed_by=user)


# ---------------------------------------------------------------------------
# 8. reject campaign variance → rejected
# ---------------------------------------------------------------------------

class TestRejectCampaignVariance:
    def test_reject_moves_to_rejected(self, node, user, org, active_budget):
        campaign = make_campaign(node, user, org=org, budget=active_budget, amount=Decimal("10500000.00"))
        submit_campaign_for_budget(campaign, submitted_by=user)
        campaign.refresh_from_db()

        review_campaign_budget_variance(campaign, decision="rejected", reviewed_by=user)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.REJECTED
        # Budget should still have no reservation
        active_budget.refresh_from_db()
        assert active_budget.reserved_amount == Decimal("0")


# ---------------------------------------------------------------------------
# 9. cancel campaign with reserved budget → releases reservation
# ---------------------------------------------------------------------------

class TestCancelCampaignWithReservation:
    def test_cancel_releases_reservation(self, node, user, org, active_budget):
        campaign = make_campaign(node, user, org=org, budget=active_budget, amount=Decimal("500000.00"))
        submit_campaign_for_budget(campaign, submitted_by=user)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.PENDING_WORKFLOW

        active_budget.refresh_from_db()
        assert active_budget.reserved_amount == Decimal("500000.00")

        cancel_campaign(campaign, cancelled_by=user)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.CANCELLED

        active_budget.refresh_from_db()
        assert active_budget.reserved_amount == Decimal("0")

    def test_cancel_terminal_status_raises(self, node, user, org):
        campaign = make_campaign(node, user, org=org)
        campaign.status = CampaignStatus.FINANCE_APPROVED
        campaign.save()
        with pytest.raises(CampaignStateError):
            cancel_campaign(campaign, cancelled_by=user)


# ---------------------------------------------------------------------------
# 10. cancel campaign without budget → just cancels
# ---------------------------------------------------------------------------

class TestCancelCampaignWithoutBudget:
    def test_cancel_draft_without_budget(self, node, user, org):
        campaign = make_campaign(node, user, org=org)
        assert campaign.status == CampaignStatus.DRAFT

        cancel_campaign(campaign, cancelled_by=user)
        campaign.refresh_from_db()
        assert campaign.status == CampaignStatus.CANCELLED
