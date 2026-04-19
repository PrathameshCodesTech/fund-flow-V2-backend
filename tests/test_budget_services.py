"""
Service-level tests for budget runtime logic.
"""
import pytest
from decimal import Decimal
from apps.budgets.models import (
    BudgetCategory, BudgetSubCategory, Budget, BudgetRule,
    BudgetConsumption, BudgetVarianceRequest,
    BudgetStatus, PeriodType, ConsumptionType, ConsumptionStatus,
    VarianceStatus, SourceType,
)
from apps.budgets.services import (
    reserve_budget,
    consume_reserved_budget,
    release_reserved_budget,
    review_variance_request,
    BudgetLimitExceeded,
    BudgetNotActiveError,
)
from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Budget Org", code="budget-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/budget-org/hq", depth=0, is_active=True,
    )


@pytest.fixture
def category(org):
    return BudgetCategory.objects.create(org=org, name="Marketing", code="marketing")


@pytest.fixture
def subcategory(category):
    return BudgetSubCategory.objects.create(category=category, name="Digital Ads", code="digital-ads")


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(email="admin@bud.com", password="pass")


@pytest.fixture
def budget(org, company, category, subcategory, admin_user):
    return Budget.objects.create(
        org=org,
        scope_node=company,
        category=category,
        subcategory=subcategory,
        financial_year="2026-27",
        period_type=PeriodType.YEARLY,
        period_start="2026-04-01",
        period_end="2027-03-31",
        allocated_amount=Decimal("50000000.00"),
        reserved_amount=Decimal("0"),
        consumed_amount=Decimal("0"),
        currency="INR",
        status=BudgetStatus.ACTIVE,
        created_by=admin_user,
    )


# ---------------------------------------------------------------------------
# calculate_projected_utilization — implicit via reserve_budget results
# ---------------------------------------------------------------------------

class TestReserveBudget:
    def test_reserve_below_warning_creates_applied_reservation(
        self, budget, admin_user,
    ):
        """
        Reserving 10M on a 50M budget (20% util) is below 80% warning
        → creates type=reserved, status=applied, increments reserved_amount.
        """
        result = reserve_budget(
            budget=budget,
            amount=Decimal("10000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
            note="Campaign launch",
        )
        assert result["status"] == "reserved"
        assert result["consumption"] is not None
        assert result["consumption"].consumption_type == ConsumptionType.RESERVED
        assert result["consumption"].status == ConsumptionStatus.APPLIED
        assert budget.reserved_amount == Decimal("10000000.00")
        assert budget.consumed_amount == Decimal("0")

    def test_reserve_between_warning_and_approval_returns_reserved_with_warning(
        self, budget, admin_user,
    ):
        """
        Reserving 10M → 20% (below warning), then 30M more → 80% total.
        80% >= 80% warning → reserved_with_warning.
        """
        # First reserve 10M → 20% utilization
        reserve_budget(
            budget=budget,
            amount=Decimal("10000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        # Now reserve 30M more → 80% projected (>= 80% warning)
        result = reserve_budget(
            budget=budget,
            amount=Decimal("30000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-002",
            requested_by=admin_user,
        )
        assert result["status"] == "reserved_with_warning"
        assert result["consumption"] is not None
        assert result["consumption"].consumption_type == ConsumptionType.RESERVED
        # 10M + 30M = 40M reserved on 50M = 80%
        assert budget.reserved_amount == Decimal("40000000.00")

    def test_reserve_between_approval_and_hard_block_creates_variance_request(
        self, budget, admin_user,
    ):
        """
        Default approval=100%. Reserve 39M → 78% (below 100% approval).
        Then reserve 12M more → 102% projected (>= 100% approval, < 110% hard block).
        → variance_required, no reservation created.
        """
        # Reserve 39M → 78% (below approval threshold)
        reserve_budget(
            budget=budget,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        # Now reserve 12M more → 102% projected (above 100% approval threshold)
        result = reserve_budget(
            budget=budget,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        assert result["status"] == "variance_required"
        assert result["consumption"] is None
        assert result["variance_request"] is not None
        assert result["variance_request"].status == VarianceStatus.PENDING
        # Reserved amount unchanged (variance, not reservation)
        assert budget.reserved_amount == Decimal("39000000.00")

    def test_reserve_above_hard_block_raises_and_does_not_increment(
        self, budget, admin_user,
    ):
        """
        Reserving 56M on a 50M budget (112% projected) exceeds 110% hard block
        → BudgetLimitExceeded, no consumption, reserved unchanged.
        """
        initial_reserved = budget.reserved_amount
        with pytest.raises(BudgetLimitExceeded) as exc_info:
            reserve_budget(
                budget=budget,
                amount=Decimal("56000000.00"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-big",
                requested_by=admin_user,
            )
        assert "hard block" in str(exc_info.value).lower()
        budget.refresh_from_db()
        assert budget.reserved_amount == initial_reserved

    def test_reserve_inactive_budget_raises(
        self, budget, admin_user,
    ):
        """Only ACTIVE budgets accept reservations."""
        budget.status = BudgetStatus.DRAFT
        budget.save()
        with pytest.raises(BudgetNotActiveError):
            reserve_budget(
                budget=budget,
                amount=Decimal("1000000.00"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-001",
                requested_by=admin_user,
            )

    def test_reserve_zero_amount_raises(
        self, budget, admin_user,
    ):
        with pytest.raises(ValueError) as exc_info:
            reserve_budget(
                budget=budget,
                amount=Decimal("0"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-001",
                requested_by=admin_user,
            )
        assert "greater than zero" in str(exc_info.value).lower()


class TestReviewVarianceRequest:
    def test_approved_variance_creates_reservation_and_increments_reserved(
        self, budget, admin_user,
    ):
        """Approved variance creates reserved consumption and increments budget.reserved_amount."""
        # Default approval=100%. Reserve 39M → 78% (below 100%, normal reservation).
        reserve_budget(
            budget=budget,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        assert budget.reserved_amount == Decimal("39000000.00")

        # Reserve 12M more → 102% projected (above 100% approval) → variance_required
        result = reserve_budget(
            budget=budget,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        variance = result["variance_request"]
        assert variance.status == VarianceStatus.PENDING
        # Reserved unchanged (variance not yet approved)
        budget.refresh_from_db()
        assert budget.reserved_amount == Decimal("39000000.00")

        # Approve
        updated = review_variance_request(
            variance_request=variance,
            decision="approved",
            reviewed_by=admin_user,
            review_note="Approved for Q2 campaign.",
        )
        assert updated.status == VarianceStatus.APPROVED
        budget.refresh_from_db()
        assert budget.reserved_amount == Decimal("51000000.00")  # 39M + 12M

        # Check consumption was created
        consumption = BudgetConsumption.objects.filter(
            budget=budget,
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            consumption_type=ConsumptionType.RESERVED,
        ).first()
        assert consumption is not None
        assert consumption.amount == Decimal("12000000.00")
        assert consumption.status == ConsumptionStatus.APPLIED

    def test_rejected_variance_does_not_change_budget(
        self, budget, admin_user,
    ):
        """Rejected variance leaves reserved_amount unchanged."""
        reserve_budget(
            budget=budget,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = reserve_budget(
            budget=budget,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        variance = result["variance_request"]
        budget.refresh_from_db()
        initial_reserved = budget.reserved_amount  # 39M

        updated = review_variance_request(
            variance_request=variance,
            decision="rejected",
            reviewed_by=admin_user,
            review_note="Budget exhausted.",
        )
        assert updated.status == VarianceStatus.REJECTED
        budget.refresh_from_db()
        assert budget.reserved_amount == initial_reserved  # still 39M

    def test_review_non_pending_raises(
        self, budget, admin_user,
    ):
        """Reviewing a non-PENDING variance raises ValueError."""
        reserve_budget(
            budget=budget,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = reserve_budget(
            budget=budget,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        variance = result["variance_request"]

        # First approve
        review_variance_request(variance, "approved", admin_user)

        # Try approving again — should raise
        with pytest.raises(ValueError) as exc_info:
            review_variance_request(variance, "approved", admin_user)
        assert "PENDING" in str(exc_info.value)


class TestConsumeReservedBudget:
    def test_consume_reduces_reserved_and_increases_consumed(
        self, budget, admin_user,
    ):
        """Consuming 5M from a 10M reservation reduces reserved, increases consumed."""
        reserve_budget(
            budget=budget,
            amount=Decimal("10000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = consume_reserved_budget(
            budget=budget,
            amount=Decimal("5000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            consumed_by=admin_user,
            note="Actual spend",
        )
        assert result["status"] == "consumed"
        budget.refresh_from_db()
        assert budget.reserved_amount == Decimal("5000000.00")
        assert budget.consumed_amount == Decimal("5000000.00")

    def test_consume_more_than_reserved_raises(
        self, budget, admin_user,
    ):
        """Cannot consume more than currently reserved."""
        reserve_budget(
            budget=budget,
            amount=Decimal("1000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        with pytest.raises(ValueError) as exc_info:
            consume_reserved_budget(
                budget=budget,
                amount=Decimal("5000000.00"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-001",
                consumed_by=admin_user,
            )
        assert "only" in str(exc_info.value).lower()


class TestReleaseReservedBudget:
    def test_release_reduces_reserved_amount(
        self, budget, admin_user,
    ):
        """Releasing 3M from a 10M reservation reduces reserved_amount."""
        reserve_budget(
            budget=budget,
            amount=Decimal("10000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = release_reserved_budget(
            budget=budget,
            amount=Decimal("3000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            released_by=admin_user,
            note="Campaign cancelled",
        )
        assert result["status"] == "released"
        budget.refresh_from_db()
        assert budget.reserved_amount == Decimal("7000000.00")
        assert budget.consumed_amount == Decimal("0")

    def test_release_more_than_reserved_raises(
        self, budget, admin_user,
    ):
        """Cannot release more than currently reserved."""
        reserve_budget(
            budget=budget,
            amount=Decimal("1000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        with pytest.raises(ValueError) as exc_info:
            release_reserved_budget(
                budget=budget,
                amount=Decimal("5000000.00"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-001",
                released_by=admin_user,
            )
        assert "only" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestBudgetModel:
    def test_available_amount(self, budget):
        """available_amount = allocated - reserved - consumed."""
        budget.reserved_amount = Decimal("10000000.00")
        budget.consumed_amount = Decimal("5000000.00")
        budget.save()
        assert budget.available_amount == Decimal("35000000.00")

    def test_available_amount_never_negative(self, budget):
        """available_amount floors at 0."""
        budget.reserved_amount = Decimal("60000000.00")
        budget.consumed_amount = Decimal("0")
        budget.save()
        assert budget.available_amount == Decimal("0")

    def test_utilization_percent(self, budget):
        """utilization_percent = (reserved+consumed) / allocated * 100."""
        budget.reserved_amount = Decimal("10000000.00")
        budget.consumed_amount = Decimal("0")
        budget.save()
        assert budget.utilization_percent == Decimal("20.00")

    def test_utilization_percent_zero_allocated(self, budget):
        """Zero allocated returns 0 utilization."""
        budget.allocated_amount = Decimal("0")
        budget.save()
        assert budget.utilization_percent == Decimal("0")

    def test_unique_constraint_on_budget(
        self, org, company, category, subcategory, admin_user,
    ):
        """Duplicate budget allocation raises IntegrityError."""
        Budget.objects.create(
            org=org, scope_node=company, category=category,
            subcategory=subcategory,
            financial_year="2026-27",
            period_type=PeriodType.YEARLY,
            period_start="2026-04-01",
            period_end="2027-03-31",
            allocated_amount=Decimal("10000000.00"),
            status=BudgetStatus.ACTIVE,
            created_by=admin_user,
        )
        with pytest.raises(Exception):  # IntegrityError
            Budget.objects.create(
                org=org, scope_node=company, category=category,
                subcategory=subcategory,
                financial_year="2026-27",
                period_type=PeriodType.YEARLY,
                period_start="2026-04-01",
                period_end="2027-03-31",
                allocated_amount=Decimal("20000000.00"),
                status=BudgetStatus.ACTIVE,
                created_by=admin_user,
            )


class TestBudgetRuleValidation:
    def test_warning_must_be_less_than_approval(self, budget):
        """warning >= approval should raise during clean()."""
        rule = BudgetRule(
            budget=budget,
            warning_threshold_percent=Decimal("90.00"),
            approval_threshold_percent=Decimal("80.00"),  # invalid
        )
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError):
            rule.clean()

    def test_approval_must_be_lte_hard_block(self, budget):
        """approval > hard_block should raise during clean()."""
        rule = BudgetRule(
            budget=budget,
            warning_threshold_percent=Decimal("70.00"),
            approval_threshold_percent=Decimal("120.00"),  # > 110 hard block
            hard_block_threshold_percent=Decimal("110.00"),
        )
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError):
            rule.clean()
