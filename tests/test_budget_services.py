"""
Service-level tests for budget runtime logic.
"""
import pytest
from decimal import Decimal
from apps.budgets.models import (
    BudgetCategory, BudgetSubCategory, Budget, BudgetLine, BudgetRule,
    BudgetConsumption, BudgetVarianceRequest,
    BudgetStatus, PeriodType, ConsumptionType, ConsumptionStatus,
    VarianceStatus, SourceType,
)
from apps.budgets.services import (
    reserve_budget_line,
    consume_reserved_budget_line,
    release_reserved_budget_line,
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
def budget(org, company, admin_user):
    return Budget.objects.create(
        org=org,
        scope_node=company,
        name="FY27 Marketing HQ",
        code="FY27-MKT-HQ",
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


@pytest.fixture
def budget_line(budget, category, subcategory):
    return BudgetLine.objects.create(
        budget=budget,
        category=category,
        subcategory=subcategory,
        allocated_amount=Decimal("50000000.00"),
    )


# ---------------------------------------------------------------------------
# reserve_budget_line
# ---------------------------------------------------------------------------

class TestReserveBudgetLine:
    def test_reserve_below_warning_creates_applied_reservation(
        self, budget, budget_line, admin_user,
    ):
        """
        Reserving 10M on a 50M line (20% util) is below 80% warning
        → creates type=reserved, status=applied, increments both line and header.
        """
        result = reserve_budget_line(
            line=budget_line,
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
        budget_line.refresh_from_db()
        budget.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("10000000.00")
        assert budget.reserved_amount == Decimal("10000000.00")

    def test_reserve_between_warning_and_approval_returns_reserved_with_warning(
        self, budget, budget_line, admin_user,
    ):
        """
        Reserving 40M on a 50M line → 80% projected (≥ 80% warning threshold)
        → reserved_with_warning.
        """
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("10000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = reserve_budget_line(
            line=budget_line,
            amount=Decimal("30000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-002",
            requested_by=admin_user,
        )
        assert result["status"] == "reserved_with_warning"
        budget_line.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("40000000.00")

    def test_reserve_between_approval_and_hard_block_creates_variance_request(
        self, budget, budget_line, admin_user,
    ):
        """
        Reserving 12M when 39M already reserved (102% projected) → variance_required.
        """
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = reserve_budget_line(
            line=budget_line,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        assert result["status"] == "variance_required"
        assert result["consumption"] is None
        assert result["variance_request"] is not None
        assert result["variance_request"].status == VarianceStatus.PENDING
        assert result["variance_request"].budget_line == budget_line
        # Reserved unchanged
        budget_line.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("39000000.00")

    def test_reserve_above_hard_block_raises_and_does_not_increment(
        self, budget, budget_line, admin_user,
    ):
        """112% projected → BudgetLimitExceeded."""
        with pytest.raises(BudgetLimitExceeded) as exc_info:
            reserve_budget_line(
                line=budget_line,
                amount=Decimal("56000000.00"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-big",
                requested_by=admin_user,
            )
        assert "hard block" in str(exc_info.value).lower()
        budget_line.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("0")

    def test_reserve_inactive_budget_raises(
        self, budget, budget_line, admin_user,
    ):
        budget.status = BudgetStatus.DRAFT
        budget.save()
        with pytest.raises(BudgetNotActiveError):
            reserve_budget_line(
                line=budget_line,
                amount=Decimal("1000000.00"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-001",
                requested_by=admin_user,
            )

    def test_reserve_zero_amount_raises(
        self, budget, budget_line, admin_user,
    ):
        with pytest.raises(ValueError) as exc_info:
            reserve_budget_line(
                line=budget_line,
                amount=Decimal("0"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-001",
                requested_by=admin_user,
            )
        assert "greater than zero" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# review_variance_request (with budget_line)
# ---------------------------------------------------------------------------

class TestReviewVarianceRequest:
    def test_approved_variance_creates_reservation_and_increments_both(
        self, budget, budget_line, admin_user,
    ):
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = reserve_budget_line(
            line=budget_line,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        variance = result["variance_request"]
        assert variance.status == VarianceStatus.PENDING

        updated = review_variance_request(
            variance_request=variance,
            decision="approved",
            reviewed_by=admin_user,
            review_note="Approved for Q2.",
        )
        assert updated.status == VarianceStatus.APPROVED
        budget_line.refresh_from_db()
        budget.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("51000000.00")
        assert budget.reserved_amount == Decimal("51000000.00")

        consumption = BudgetConsumption.objects.filter(
            budget_line=budget_line,
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            consumption_type=ConsumptionType.RESERVED,
        ).filter(
            note__icontains="Approved variance"
        ).first()
        assert consumption is not None
        assert consumption.amount == Decimal("12000000.00")

    def test_rejected_variance_does_not_change_budget(
        self, budget, budget_line, admin_user,
    ):
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = reserve_budget_line(
            line=budget_line,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        variance = result["variance_request"]
        budget_line.refresh_from_db()
        initial_reserved = budget_line.reserved_amount  # 39M

        updated = review_variance_request(
            variance_request=variance,
            decision="rejected",
            reviewed_by=admin_user,
            review_note="Budget exhausted.",
        )
        assert updated.status == VarianceStatus.REJECTED
        budget_line.refresh_from_db()
        assert budget_line.reserved_amount == initial_reserved

    def test_review_non_pending_raises(
        self, budget, budget_line, admin_user,
    ):
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("39000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = reserve_budget_line(
            line=budget_line,
            amount=Decimal("12000000.00"),
            source_type=SourceType.INVOICE,
            source_id="inv-001",
            requested_by=admin_user,
        )
        variance = result["variance_request"]
        review_variance_request(variance, "approved", admin_user)

        with pytest.raises(ValueError) as exc_info:
            review_variance_request(variance, "approved", admin_user)
        assert "PENDING" in str(exc_info.value)


# ---------------------------------------------------------------------------
# consume_reserved_budget_line
# ---------------------------------------------------------------------------

class TestConsumeReservedBudgetLine:
    def test_consume_reduces_reserved_and_increases_consumed(
        self, budget, budget_line, admin_user,
    ):
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("10000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = consume_reserved_budget_line(
            line=budget_line,
            amount=Decimal("5000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            consumed_by=admin_user,
            note="Actual spend",
        )
        assert result["status"] == "consumed"
        budget_line.refresh_from_db()
        budget.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("5000000.00")
        assert budget_line.consumed_amount == Decimal("5000000.00")
        assert budget.reserved_amount == Decimal("5000000.00")
        assert budget.consumed_amount == Decimal("5000000.00")

    def test_consume_more_than_reserved_raises(
        self, budget, budget_line, admin_user,
    ):
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("1000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        with pytest.raises(ValueError) as exc_info:
            consume_reserved_budget_line(
                line=budget_line,
                amount=Decimal("5000000.00"),
                source_type=SourceType.CAMPAIGN,
                source_id="camp-001",
                consumed_by=admin_user,
            )
        assert "only" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# release_reserved_budget_line
# ---------------------------------------------------------------------------

class TestReleaseReservedBudgetLine:
    def test_release_reduces_reserved_amount(
        self, budget, budget_line, admin_user,
    ):
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("10000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        result = release_reserved_budget_line(
            line=budget_line,
            amount=Decimal("3000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            released_by=admin_user,
            note="Campaign cancelled",
        )
        assert result["status"] == "released"
        budget_line.refresh_from_db()
        budget.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("7000000.00")
        assert budget.reserved_amount == Decimal("7000000.00")

    def test_release_more_than_reserved_raises(
        self, budget, budget_line, admin_user,
    ):
        reserve_budget_line(
            line=budget_line,
            amount=Decimal("1000000.00"),
            source_type=SourceType.CAMPAIGN,
            source_id="camp-001",
            requested_by=admin_user,
        )
        with pytest.raises(ValueError) as exc_info:
            release_reserved_budget_line(
                line=budget_line,
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
        budget.reserved_amount = Decimal("10000000.00")
        budget.consumed_amount = Decimal("5000000.00")
        budget.save()
        assert budget.available_amount == Decimal("35000000.00")

    def test_available_amount_never_negative(self, budget):
        budget.reserved_amount = Decimal("60000000.00")
        budget.consumed_amount = Decimal("0")
        budget.save()
        assert budget.available_amount == Decimal("0")

    def test_utilization_percent(self, budget):
        budget.reserved_amount = Decimal("10000000.00")
        budget.consumed_amount = Decimal("0")
        budget.save()
        assert budget.utilization_percent == Decimal("20.00")

    def test_utilization_percent_zero_allocated(self, budget):
        budget.allocated_amount = Decimal("0")
        budget.save()
        assert budget.utilization_percent == Decimal("0")

    def test_unique_constraint_on_budget(self, org, company, admin_user):
        """Duplicate budget (same scope_node+financial_year+code) raises IntegrityError."""
        Budget.objects.create(
            org=org, scope_node=company,
            name="FY27 Marketing A", code="FY27-MKT-A",
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
                org=org, scope_node=company,
                name="FY27 Marketing A Dupe", code="FY27-MKT-A",
                financial_year="2026-27",
                period_type=PeriodType.YEARLY,
                period_start="2026-04-01",
                period_end="2027-03-31",
                allocated_amount=Decimal("20000000.00"),
                status=BudgetStatus.ACTIVE,
                created_by=admin_user,
            )

    def test_line_sum_validation(self, budget, category, subcategory):
        """BudgetLine available_amount property works correctly."""
        line = BudgetLine.objects.create(
            budget=budget,
            category=category,
            subcategory=subcategory,
            allocated_amount=Decimal("50000000.00"),
        )
        line.reserved_amount = Decimal("10000000.00")
        line.consumed_amount = Decimal("5000000.00")
        line.save()
        assert line.available_amount == Decimal("35000000.00")


class TestBudgetRuleValidation:
    def test_warning_must_be_less_than_approval(self, budget):
        rule = BudgetRule(
            budget=budget,
            warning_threshold_percent=Decimal("90.00"),
            approval_threshold_percent=Decimal("80.00"),
        )
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError):
            rule.clean()

    def test_approval_must_be_lte_hard_block(self, budget):
        rule = BudgetRule(
            budget=budget,
            warning_threshold_percent=Decimal("70.00"),
            approval_threshold_percent=Decimal("120.00"),
            hard_block_threshold_percent=Decimal("110.00"),
        )
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError):
            rule.clean()
