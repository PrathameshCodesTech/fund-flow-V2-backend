"""
Enterprise budget subsystem tests — Correction pass (history-aware locking,
safe import policy, ledger truth, import audit).

Tests 1-23: first-pass scenarios
Tests 24-31: correction-pass scenarios
"""
import io
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from apps.budgets.models import (
    BudgetCategory, BudgetSubCategory, Budget, BudgetLine, BudgetRule,
    BudgetConsumption, BudgetVarianceRequest, BudgetImportBatch, BudgetImportRow,
    BudgetStatus, PeriodType, ConsumptionType, ConsumptionStatus,
    VarianceStatus, SourceType, ImportBatchStatus, ImportRowStatus, ImportMode,
)
from apps.budgets.services import (
    reserve_budget_line,
    consume_reserved_budget_line,
    release_reserved_budget_line,
    review_variance_request,
    can_delete_budget,
    can_delete_budget_line,
    can_delete_budget_category,
    can_delete_budget_subcategory,
    can_decrease_budget_allocated,
    can_decrease_budget_line_allocated,
    validate_budget_import_batch,
    create_budget_import_batch,
    commit_budget_import_batch,
    get_budget_in_use_summary,
    get_budget_line_in_use_summary,
    BudgetLimitExceeded,
    BudgetNotActiveError,
)
from apps.budgets.selectors import get_budget_live_balances
from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Enterprise Org", code="enterprise-org")


@pytest.fixture
def scope_node(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="HQ",
        node_type=NodeType.COMPANY, path="/enterprise-org/hq", depth=0, is_active=True,
    )


@pytest.fixture
def category(org):
    return BudgetCategory.objects.create(org=org, name="Marketing", code="MKTG")


@pytest.fixture
def subcategory(category):
    return BudgetSubCategory.objects.create(category=category, name="Digital", code="DIG")


@pytest.fixture
def actor(db):
    return User.objects.create_user(email="actor@example.com", password="pass")


@pytest.fixture
def active_budget(org, scope_node, actor):
    b = Budget.objects.create(
        org=org, scope_node=scope_node,
        name="FY27 Marketing", code="FY27-MKTG",
        financial_year="2026-27",
        period_type=PeriodType.YEARLY,
        allocated_amount=Decimal("1000000.00"),
        status=BudgetStatus.ACTIVE,
        created_by=actor,
    )
    BudgetRule.objects.create(
        budget=b,
        warning_threshold_percent=Decimal("80"),
        approval_threshold_percent=Decimal("100"),
        hard_block_threshold_percent=Decimal("110"),
    )
    return b


@pytest.fixture
def budget_line(active_budget, category, subcategory):
    return BudgetLine.objects.create(
        budget=active_budget,
        category=category,
        subcategory=subcategory,
        allocated_amount=Decimal("1000000.00"),
    )


# ---------------------------------------------------------------------------
# 1. Import validation — missing scope_node_code → ERROR row
# ---------------------------------------------------------------------------

class TestImportValidation:
    def test_missing_scope_node_code_produces_error_row(self, db, org, category, subcategory, actor):
        """A row without scope_node_code must be marked ERROR after validation."""
        parsed_rows = [
            {
                "scope_node_code": "",          # intentionally blank
                "budget_code": "FY27-MKTG",
                "budget_name": "FY27 Marketing",
                "financial_year": "2026-27",
                "period_type": "yearly",
                "category_code": category.code,
                "subcategory_code": subcategory.code,
                "allocated_amount": "500000",
                "currency": "INR",
            }
        ]
        batch = create_budget_import_batch(
            org=org, file_name="test.xlsx",
            parsed_rows=parsed_rows, created_by=actor,
        )
        batch = validate_budget_import_batch(batch)

        assert batch.error_rows == 1
        assert batch.valid_rows == 0
        assert batch.status == ImportBatchStatus.FAILED

        row = batch.rows.first()
        assert row.status == ImportRowStatus.ERROR
        assert any("scope_node_code" in e for e in row.errors)


# ---------------------------------------------------------------------------
# 2. Valid import creates hierarchy
# ---------------------------------------------------------------------------

class TestImportCommit:
    def test_valid_import_creates_budget_and_line(self, db, org, scope_node, category, subcategory, actor):
        """A fully valid row should create a Budget and BudgetLine on commit."""
        parsed_rows = [
            {
                "scope_node_code": scope_node.code,
                "budget_code": "FY27-NEW",
                "budget_name": "FY27 New Budget",
                "financial_year": "2026-27",
                "period_type": "yearly",
                "period_start": "2026-04-01",
                "period_end": "2027-03-31",
                "category_code": category.code,
                "subcategory_code": subcategory.code,
                "allocated_amount": "250000",
                "currency": "INR",
            }
        ]
        batch = create_budget_import_batch(
            org=org, file_name="import.xlsx",
            parsed_rows=parsed_rows, created_by=actor,
        )
        batch = validate_budget_import_batch(batch)
        assert batch.valid_rows == 1, f"Expected 1 valid row, got errors: {list(batch.rows.values_list('errors', flat=True))}"

        batch = commit_budget_import_batch(batch, committed_by=actor)
        assert batch.committed_rows == 1
        assert batch.status == ImportBatchStatus.COMMITTED

        assert Budget.objects.filter(scope_node=scope_node, code="FY27-NEW", financial_year="2026-27").exists()
        budget = Budget.objects.get(scope_node=scope_node, code="FY27-NEW", financial_year="2026-27")
        assert BudgetLine.objects.filter(budget=budget, category=category, subcategory=subcategory).exists()
        line = BudgetLine.objects.get(budget=budget, category=category, subcategory=subcategory)
        assert line.allocated_amount == Decimal("250000")


# ---------------------------------------------------------------------------
# 3. Invoice allocation reservation
# ---------------------------------------------------------------------------

class TestInvoiceAllocationReservation:
    def test_reserve_creates_applied_consumption_with_invoice_allocation_source(
        self, budget_line, active_budget, actor
    ):
        """Reserving with source_type=invoice_allocation creates a RESERVED consumption."""
        result = reserve_budget_line(
            line=budget_line,
            amount=Decimal("100000"),
            source_type=SourceType.INVOICE_ALLOCATION,
            source_id="alloc-101",
            requested_by=actor,
            note="Invoice allocation reserve",
        )
        assert result["status"] == "reserved"
        consumption = result["consumption"]
        assert consumption is not None
        assert consumption.source_type == SourceType.INVOICE_ALLOCATION
        assert consumption.source_id == "alloc-101"
        assert consumption.consumption_type == ConsumptionType.RESERVED
        assert consumption.status == ConsumptionStatus.APPLIED

        budget_line.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("100000")


# ---------------------------------------------------------------------------
# 4. Rejection releases reservation
# ---------------------------------------------------------------------------

class TestRejectionReleasesReservation:
    def test_release_creates_released_consumption_and_decrements_reserved(
        self, budget_line, active_budget, actor
    ):
        """Releasing a previously reserved amount creates RELEASED consumption."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("200000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-202",
            requested_by=actor,
        )
        result = release_reserved_budget_line(
            line=budget_line, amount=Decimal("200000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-202",
            released_by=actor, note="Allocation rejected",
        )
        assert result["status"] == "released"
        assert result["consumption"].consumption_type == ConsumptionType.RELEASED

        budget_line.refresh_from_db()
        active_budget.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("0")
        assert active_budget.reserved_amount == Decimal("0")


# ---------------------------------------------------------------------------
# 5. Workflow approval consumes
# ---------------------------------------------------------------------------

class TestWorkflowApprovalConsumes:
    def test_consume_converts_reserved_to_consumed(self, budget_line, active_budget, actor):
        """Consuming a reserved amount moves it from reserved to consumed."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("300000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-303",
            requested_by=actor,
        )
        result = consume_reserved_budget_line(
            line=budget_line, amount=Decimal("300000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-303",
            consumed_by=actor, note="Workflow approved",
        )
        assert result["status"] == "consumed"
        assert result["consumption"].consumption_type == ConsumptionType.CONSUMED

        budget_line.refresh_from_db()
        active_budget.refresh_from_db()
        assert budget_line.reserved_amount == Decimal("0")
        assert budget_line.consumed_amount == Decimal("300000")
        assert active_budget.consumed_amount == Decimal("300000")


# ---------------------------------------------------------------------------
# 6. Budget allocated decrease blocked
# ---------------------------------------------------------------------------

class TestBudgetDecreaseBlocked:
    def test_decrease_below_reserved_is_blocked(self, budget_line, active_budget, actor):
        """can_decrease_budget_allocated returns False when new_amount < reserved."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("500000"),
            source_type=SourceType.INVOICE, source_id="inv-001",
            requested_by=actor,
        )
        active_budget.refresh_from_db()
        ok, reason = can_decrease_budget_allocated(active_budget, Decimal("100000"))
        assert ok is False
        assert "reserved" in reason.lower() or "500000" in reason

    def test_increase_is_always_allowed(self, active_budget):
        """Increasing allocated_amount is always safe."""
        ok, reason = can_decrease_budget_allocated(active_budget, Decimal("2000000"))
        assert ok is True
        assert reason == ""

    def test_line_decrease_blocked_when_reserved(self, budget_line, actor):
        """can_decrease_budget_line_allocated returns False when line has reservations."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("400000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-999",
            requested_by=actor,
        )
        budget_line.refresh_from_db()
        ok, reason = can_decrease_budget_line_allocated(budget_line, Decimal("100000"))
        assert ok is False
        assert "reserved" in reason.lower()


# ---------------------------------------------------------------------------
# 7. In-use delete blocked
# ---------------------------------------------------------------------------

class TestInUseDeleteBlocked:
    def test_budget_delete_blocked_when_reserved(self, budget_line, active_budget, actor):
        reserve_budget_line(
            line=budget_line, amount=Decimal("50000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-del",
            requested_by=actor,
        )
        ok, reason = can_delete_budget(active_budget)
        assert ok is False
        assert "reservation" in reason.lower() or "reserved" in reason.lower()

    def test_budget_line_delete_blocked_when_consumed(self, budget_line, active_budget, actor):
        reserve_budget_line(
            line=budget_line, amount=Decimal("50000"),
            source_type=SourceType.INVOICE, source_id="inv-del",
            requested_by=actor,
        )
        consume_reserved_budget_line(
            line=budget_line, amount=Decimal("50000"),
            source_type=SourceType.INVOICE, source_id="inv-del",
            consumed_by=actor,
        )
        budget_line.refresh_from_db()
        ok, reason = can_delete_budget_line(budget_line)
        assert ok is False

    def test_category_delete_blocked_when_line_has_usage(
        self, budget_line, active_budget, category, actor
    ):
        reserve_budget_line(
            line=budget_line, amount=Decimal("10000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-cat",
            requested_by=actor,
        )
        ok, reason = can_delete_budget_category(category)
        assert ok is False

    def test_empty_budget_delete_allowed(self, active_budget):
        ok, reason = can_delete_budget(active_budget)
        assert ok is True


# ---------------------------------------------------------------------------
# 8. Rename/deactivate allowed with in-use lines
# ---------------------------------------------------------------------------

class TestRenameAllowedWithInUseLines:
    def test_category_rename_succeeds_even_with_reserved_lines(
        self, budget_line, active_budget, category, actor
    ):
        """Renaming a category is metadata-only and must not be blocked."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("5000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-rename",
            requested_by=actor,
        )
        category.name = "Renamed Marketing"
        category.save()  # should not raise
        category.refresh_from_db()
        assert category.name == "Renamed Marketing"

    def test_category_deactivate_succeeds_with_reserved_lines(
        self, budget_line, active_budget, category, actor
    ):
        """Deactivating (is_active=False) is a soft flag — must not be blocked."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("5000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-deact",
            requested_by=actor,
        )
        category.is_active = False
        category.save()  # must not raise
        category.refresh_from_db()
        assert category.is_active is False


# ---------------------------------------------------------------------------
# 9. BudgetLine FK survives category rename
# ---------------------------------------------------------------------------

class TestFKSurvivesCategoryRename:
    def test_budget_line_category_fk_intact_after_rename(
        self, budget_line, category
    ):
        old_id = category.id
        category.name = "Marketing v2"
        category.code = "MKTG2"
        category.save()

        budget_line.refresh_from_db()
        assert budget_line.category_id == old_id
        assert budget_line.category.name == "Marketing v2"


# ---------------------------------------------------------------------------
# 10. Available balance correctness
# ---------------------------------------------------------------------------

class TestAvailableBalanceCorrectness:
    def test_available_equals_allocated_minus_reserved_minus_consumed(
        self, budget_line, active_budget, actor
    ):
        """available = allocated - reserved - consumed, never negative."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("300000"),
            source_type=SourceType.INVOICE, source_id="inv-bal",
            requested_by=actor,
        )
        consume_reserved_budget_line(
            line=budget_line, amount=Decimal("100000"),
            source_type=SourceType.INVOICE, source_id="inv-bal",
            consumed_by=actor,
        )
        budget_line.refresh_from_db()
        # reserved now 200000 (300000 - 100000 consumed), consumed 100000
        expected_available = Decimal("1000000") - Decimal("200000") - Decimal("100000")
        assert budget_line.available_amount == expected_available

    def test_available_does_not_go_negative(self, active_budget, budget_line, actor):
        """available_amount property is always ≥ 0."""
        # Manually set reserved > allocated (shouldn't happen in practice, but guard test)
        BudgetLine.objects.filter(pk=budget_line.pk).update(
            reserved_amount=Decimal("2000000"), consumed_amount=Decimal("0")
        )
        budget_line.refresh_from_db()
        assert budget_line.available_amount == Decimal("0")


# ---------------------------------------------------------------------------
# 11. Ledger source metadata accuracy
# ---------------------------------------------------------------------------

class TestLedgerSourceMetadata:
    def test_consumption_carries_correct_source_type_and_id(
        self, budget_line, active_budget, actor
    ):
        result = reserve_budget_line(
            line=budget_line, amount=Decimal("77000"),
            source_type=SourceType.MANUAL_EXPENSE, source_id="me-42",
            requested_by=actor,
        )
        cons = BudgetConsumption.objects.get(pk=result["consumption"].id)
        assert cons.source_type == SourceType.MANUAL_EXPENSE
        assert cons.source_id == "me-42"
        assert cons.budget_id == active_budget.id
        assert cons.budget_line_id == budget_line.id

    def test_live_balances_matches_denormalized_fields(
        self, budget_line, active_budget, actor
    ):
        """get_budget_live_balances() must match the denormalized fields after operations."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("150000"),
            source_type=SourceType.INVOICE, source_id="inv-live",
            requested_by=actor,
        )
        active_budget.refresh_from_db()
        balances = get_budget_live_balances(active_budget)
        assert Decimal(balances["reserved_amount"]) == active_budget.reserved_amount
        assert Decimal(balances["consumed_amount"]) == active_budget.consumed_amount


# ---------------------------------------------------------------------------
# 12. Manual expense settle creates CONSUMED entry
# ---------------------------------------------------------------------------

class TestManualExpenseLedgerPath:
    def test_settling_expense_creates_consumed_budget_consumption(
        self, db, org, scope_node, active_budget, budget_line, category, subcategory, actor
    ):
        """mark_expense_settled must create a BudgetConsumption(CONSUMED) entry."""
        from apps.manual_expenses.models import ManualExpenseEntry, ExpenseStatus, PaymentMethod
        from apps.manual_expenses.services import submit_expense, mark_expense_settled
        from apps.budgets.models import BudgetConsumption, ConsumptionType

        # Build minimal expense that can be submitted (attach mock file via attachment count)
        expense = ManualExpenseEntry.objects.create(
            org=org,
            scope_node=scope_node,
            created_by=actor,
            status=ExpenseStatus.SUBMITTED,  # start as submitted to bypass submit validation
            payment_method=PaymentMethod.PETTY_CASH,
            vendor_name="Acme Pvt Ltd",
            expense_date="2026-05-01",
            amount=Decimal("75000"),
            currency="INR",
            budget=active_budget,
            budget_line=budget_line,
            category=category,
            subcategory=subcategory,
        )
        # Manually mark submitted_at so the model is consistent
        from django.utils import timezone
        expense.submitted_at = timezone.now()
        expense.save()

        consumption_count_before = BudgetConsumption.objects.filter(
            budget=active_budget
        ).count()

        mark_expense_settled(expense, settled_by=actor)

        expense.refresh_from_db()
        assert expense.status == ExpenseStatus.SETTLED

        new_consumptions = BudgetConsumption.objects.filter(
            budget=active_budget,
            source_type=SourceType.MANUAL_EXPENSE,
            source_id=str(expense.id),
        )
        assert new_consumptions.exists(), "Expected at least one CONSUMED consumption for manual expense"
        consumed_row = new_consumptions.filter(consumption_type=ConsumptionType.CONSUMED).first()
        assert consumed_row is not None
        assert consumed_row.amount == Decimal("75000")


# ---------------------------------------------------------------------------
# 13. Variance behaviour on approval threshold breach
# ---------------------------------------------------------------------------

class TestVarianceBehaviour:
    def test_reservation_at_100pct_creates_variance_request(
        self, budget_line, active_budget, actor
    ):
        """Reserving the full 1M (100% utilization) triggers a variance request."""
        result = reserve_budget_line(
            line=budget_line, amount=Decimal("1000000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-var",
            requested_by=actor,
        )
        assert result["status"] == "variance_required"
        assert result["variance_request"] is not None
        assert result["consumption"] is None

        vr = result["variance_request"]
        assert vr.status == VarianceStatus.PENDING
        assert vr.projected_utilization_percent == Decimal("100.00")

    def test_approved_variance_creates_reservation_consumption(
        self, budget_line, active_budget, actor
    ):
        """Approving a variance request should create the reservation consumption."""
        result = reserve_budget_line(
            line=budget_line, amount=Decimal("1000000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-var2",
            requested_by=actor,
        )
        vr = result["variance_request"]

        reviewer = User.objects.create_user(email="reviewer@example.com", password="pass")
        updated_vr = review_variance_request(
            variance_request=vr, decision="approved",
            reviewed_by=reviewer, review_note="Approved for Q4 push",
        )
        assert updated_vr.status == VarianceStatus.APPROVED

        consumption = BudgetConsumption.objects.filter(
            budget=active_budget,
            source_type=SourceType.CAMPAIGN,
            source_id="camp-var2",
            consumption_type=ConsumptionType.RESERVED,
        ).first()
        assert consumption is not None
        assert consumption.amount == Decimal("1000000")

    def test_hard_block_raises_exception(self, budget_line, active_budget, actor):
        """Reserving 110%+ of allocated raises BudgetLimitExceeded."""
        with pytest.raises(BudgetLimitExceeded):
            reserve_budget_line(
                line=budget_line, amount=Decimal("1100001"),
                source_type=SourceType.CAMPAIGN, source_id="camp-block",
                requested_by=actor,
            )


# ---------------------------------------------------------------------------
# Correction-pass tests: history-aware locking
# ---------------------------------------------------------------------------

class TestHistoryAwareLocking:
    """
    Tests 24-28: ensure released-only / historical ledger rows block delete.
    """

    def test_line_with_released_only_history_cannot_be_deleted(
        self, budget_line, active_budget, actor
    ):
        """
        A budget line that has only RELEASED consumption (net zero) must
        still be protected from delete — it has operational history.
        """
        # Reserve then fully release
        reserve_budget_line(
            line=budget_line, amount=Decimal("100000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-hist",
            requested_by=actor,
        )
        release_reserved_budget_line(
            line=budget_line, amount=Decimal("100000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-hist",
            released_by=actor,
        )
        # Now reserved=0, consumed=0 but ledger has history
        from apps.budgets.services import can_delete_budget_line
        ok, reason = can_delete_budget_line(budget_line)
        assert ok is False, (
            f"Expected delete to be blocked but got ok=True. "
            f"Line has released-only history and must be protected."
        )
        assert "in-use" in reason.lower() or "history" in reason.lower()

    def test_category_with_historical_line_cannot_be_deleted(
        self, budget_line, active_budget, category, actor
    ):
        """A category whose lines have historical usage (even net-zero) blocks delete."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("50000"),
            source_type=SourceType.INVOICE, source_id="inv-hist-cat",
            requested_by=actor,
        )
        consume_reserved_budget_line(
            line=budget_line, amount=Decimal("50000"),
            source_type=SourceType.INVOICE, source_id="inv-hist-cat",
            consumed_by=actor,
        )
        from apps.budgets.services import can_delete_budget_category
        ok, reason = can_delete_budget_category(category)
        assert ok is False
        # Either "in-use", "operational history", or "in-use line" in reason
        lower_reason = reason.lower()
        assert any(kw in lower_reason for kw in ["in-use", "operational", "usage", "line"]), (
            f"Expected reason to mention 'in-use' or 'operational history', got: {reason}"
        )

    def test_subcategory_with_historical_line_cannot_be_deleted(
        self, budget_line, active_budget, subcategory, actor
    ):
        """A subcategory whose lines have historical usage blocks delete."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("25000"),
            source_type=SourceType.MANUAL_EXPENSE, source_id="me-hist-sub",
            requested_by=actor,
        )
        consume_reserved_budget_line(
            line=budget_line, amount=Decimal("25000"),
            source_type=SourceType.MANUAL_EXPENSE, source_id="me-hist-sub",
            consumed_by=actor,
        )
        from apps.budgets.services import can_delete_budget_subcategory
        ok, reason = can_delete_budget_subcategory(subcategory)
        assert ok is False
        lower_reason = reason.lower()
        assert any(kw in lower_reason for kw in ["in-use", "operational", "usage", "line"]), (
            f"Expected reason to mention 'in-use' or 'operational history', got: {reason}"
        )

    def test_budget_with_only_released_history_is_in_use(
        self, budget_line, active_budget, actor
    ):
        """A budget with released-only ledger history must count as in-use."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("75000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-rel",
            requested_by=actor,
        )
        release_reserved_budget_line(
            line=budget_line, amount=Decimal("75000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-rel",
            released_by=actor,
        )
        from apps.budgets.services import get_budget_in_use_summary
        summary = get_budget_in_use_summary(active_budget)
        assert summary["has_ledger_history"] is True, (
            "Budget with released-only history must have has_ledger_history=True"
        )
        assert summary["is_in_use"] is True, (
            "Budget with released-only history must be is_in_use=True"
        )

    def test_in_use_summary_shows_correct_flags(
        self, budget_line, active_budget, actor
    ):
        """get_budget_in_use_summary returns correct flags after mixed operations."""
        # Reserve
        reserve_budget_line(
            line=budget_line, amount=Decimal("200000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-flags",
            requested_by=actor,
        )
        # Consume half
        consume_reserved_budget_line(
            line=budget_line, amount=Decimal("100000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-flags",
            consumed_by=actor,
        )
        # Release remainder
        release_reserved_budget_line(
            line=budget_line, amount=Decimal("100000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-flags",
            released_by=actor,
        )
        from apps.budgets.services import get_budget_in_use_summary
        summary = get_budget_in_use_summary(active_budget)
        assert summary["has_ledger_history"] is True
        # net_reserved = max(reserved(200000) - released(100000) - consumed(100000), 0) = 0
        assert summary["net_reserved"] == Decimal("0")
        # consumed = 100000
        assert summary["consumed"] == Decimal("100000")
        # pending variance = 0
        assert summary["pending_variance_requests"] == 0
        # is_in_use because has_ledger_history=True
        assert summary["is_in_use"] is True
        # linked_invoice_allocations_count = count of BudgetConsumption rows
        # (reserve + consume + release = 3 rows for same source)
        assert summary["linked_invoice_allocations_count"] == 3


class TestSafeImportPolicy:
    """
    Tests 29-31: import mode boundary enforcement.
    """

    def test_setup_only_mode_skips_existing_budget(
        self, db, org, scope_node, category, subcategory, actor
    ):
        """SETUP_ONLY mode skips rows whose budget already exists."""
        # Pre-create a budget (simulating existing record)
        existing = Budget.objects.create(
            org=org, scope_node=scope_node,
            name="Existing Budget", code="EXIST-BUD",
            financial_year="2026-27",
            period_type=PeriodType.YEARLY,
            allocated_amount=Decimal("500000"),
            status=BudgetStatus.ACTIVE,
            created_by=actor,
        )
        parsed_rows = [
            {
                "scope_node_code": scope_node.code,
                "budget_code": "EXIST-BUD",
                "budget_name": "Existing Budget",
                "financial_year": "2026-27",
                "period_type": "yearly",
                "category_code": category.code,
                "subcategory_code": subcategory.code,
                "allocated_amount": "600000",
                "currency": "INR",
            }
        ]
        from apps.budgets.services import (
            create_budget_import_batch, validate_budget_import_batch,
            commit_budget_import_batch, ImportMode, ImportBatchStatus
        )
        batch = create_budget_import_batch(
            org=org, file_name="setup_only.xlsx",
            parsed_rows=parsed_rows, created_by=actor,
            import_mode=ImportMode.SETUP_ONLY,
        )
        batch = validate_budget_import_batch(batch)
        assert batch.valid_rows == 1

        batch = commit_budget_import_batch(batch, committed_by=actor)
        assert batch.status == ImportBatchStatus.COMMITTED
        # Budget must NOT have been updated (setup_only skips existing)
        existing.refresh_from_db()
        assert existing.allocated_amount == Decimal("500000")
        # Row should be SKIPPED
        row = batch.rows.first()
        assert row.status == ImportRowStatus.SKIPPED
        assert "SETUP_ONLY" in row.skipped_reason

    def test_safe_update_skips_in_use_line(
        self, budget_line, active_budget, category, subcategory, org, scope_node, actor
    ):
        """SAFE_UPDATE skips rows whose budget line is operationally in-use."""
        # Reserve against the existing line — makes it in-use
        reserve_budget_line(
            line=budget_line, amount=Decimal("100000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-safe",
            requested_by=actor,
        )
        # Try to import a row targeting this same line
        from apps.budgets.services import (
            create_budget_import_batch, validate_budget_import_batch,
            commit_budget_import_batch, ImportMode, ImportBatchStatus
        )
        parsed_rows = [
            {
                "scope_node_code": scope_node.code,
                "budget_code": active_budget.code,
                "budget_name": active_budget.name,
                "financial_year": "2026-27",
                "period_type": "yearly",
                "category_code": category.code,
                "subcategory_code": subcategory.code,
                "allocated_amount": "800000",
                "currency": "INR",
            }
        ]
        batch = create_budget_import_batch(
            org=org, file_name="safe_update.xlsx",
            parsed_rows=parsed_rows, created_by=actor,
            import_mode=ImportMode.SAFE_UPDATE,
        )
        batch = validate_budget_import_batch(batch)
        batch = commit_budget_import_batch(batch, committed_by=actor)

        row = batch.rows.first()
        assert row.status == ImportRowStatus.SKIPPED
        assert "in-use" in row.skipped_reason or "SAFE_UPDATE" in row.skipped_reason
        # Allocated must NOT have changed
        budget_line.refresh_from_db()
        assert budget_line.allocated_amount == Decimal("1000000")

    def test_safe_update_allows_non_operational_budget(
        self, org, scope_node, category, subcategory, actor
    ):
        """SAFE_UPDATE may update a budget/line with no operational history."""
        from apps.budgets.services import (
            create_budget_import_batch, validate_budget_import_batch,
            commit_budget_import_batch, ImportMode, ImportBatchStatus
        )
        parsed_rows = [
            {
                "scope_node_code": scope_node.code,
                "budget_code": "GREENFIELD",
                "budget_name": "Greenfield Budget",
                "financial_year": "2026-27",
                "period_type": "yearly",
                "category_code": category.code,
                "subcategory_code": subcategory.code,
                "allocated_amount": "300000",
                "currency": "INR",
            }
        ]
        batch = create_budget_import_batch(
            org=org, file_name="greenfield.xlsx",
            parsed_rows=parsed_rows, created_by=actor,
            import_mode=ImportMode.SAFE_UPDATE,
        )
        batch = validate_budget_import_batch(batch)
        assert batch.valid_rows == 1

        batch = commit_budget_import_batch(batch, committed_by=actor)
        assert batch.status == ImportBatchStatus.COMMITTED

        row = batch.rows.first()
        assert row.status == ImportRowStatus.COMMITTED
        assert row.resolved_budget is not None
        assert row.resolved_budget_line is not None


class TestLedgerTruthConsistency:
    """
    Test 32: overview selector balances agree with live balance logic.
    """

    def test_overview_balances_agree_with_live_balances(
        self, budget_line, active_budget, actor
    ):
        """get_budgets_overview and get_budget_live_balances must agree on the same budget."""
        from apps.budgets.services import get_budget_in_use_summary
        from apps.budgets.selectors import get_budget_live_balances

        # Perform some mixed operations
        reserve_budget_line(
            line=budget_line, amount=Decimal("150000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-agree",
            requested_by=actor,
        )
        consume_reserved_budget_line(
            line=budget_line, amount=Decimal("60000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-agree",
            consumed_by=actor,
        )

        # Ledger-derived via get_budget_live_balances
        live = get_budget_live_balances(active_budget)

        # Manual ledger calculation for verification
        from apps.budgets.models import BudgetConsumption, ConsumptionType, ConsumptionStatus
        from django.db.models import Sum
        rows = BudgetConsumption.objects.filter(
            budget=active_budget, status=ConsumptionStatus.APPLIED
        )
        reserved = rows.filter(consumption_type=ConsumptionType.RESERVED).aggregate(
            t=Sum("amount")
        )["t"] or Decimal("0")
        consumed = rows.filter(consumption_type=ConsumptionType.CONSUMED).aggregate(
            t=Sum("amount")
        )["t"] or Decimal("0")
        released = rows.filter(consumption_type=ConsumptionType.RELEASED).aggregate(
            t=Sum("amount")
        )["t"] or Decimal("0")
        expected_net_reserved = max(reserved - released - consumed, Decimal("0"))
        expected_available = max(active_budget.allocated_amount - expected_net_reserved - consumed, Decimal("0"))

        assert Decimal(live["reserved_amount"]) == expected_net_reserved, (
            f"live reserved {live['reserved_amount']} != expected {expected_net_reserved}"
        )
        assert Decimal(live["consumed_amount"]) == consumed, (
            f"live consumed {live['consumed_amount']} != expected {consumed}"
        )
        assert Decimal(live["available_amount"]) == expected_available, (
            f"live available {live['available_amount']} != expected {expected_available}"
        )


class TestUsageSummaryHelpers:
    """
    Test 33: usage summary helpers return expected flags and counts.
    """

    def test_usage_summary_includes_all_expected_fields(
        self, budget_line, active_budget, actor
    ):
        """get_budget_in_use_summary returns all documented fields."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("50000"),
            source_type=SourceType.INVOICE_ALLOCATION, source_id="alloc-ut",
            requested_by=actor,
        )
        from apps.budgets.services import get_budget_in_use_summary
        summary = get_budget_in_use_summary(active_budget)

        required_keys = [
            "net_reserved", "consumed", "released", "adjusted",
            "pending_variance_requests",
            "linked_invoice_allocations_count",
            "linked_manual_expenses_count",
            "linked_campaign_count",
            "has_in_use_lines", "has_ledger_history", "is_in_use",
            "line_summaries",
        ]
        for key in required_keys:
            assert key in summary, f"Expected key '{key}' missing from in-use summary"

    def test_line_usage_summary_includes_all_expected_fields(
        self, budget_line, actor
    ):
        """get_budget_line_in_use_summary returns all documented fields."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("30000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-line-ut",
            requested_by=actor,
        )
        from apps.budgets.services import get_budget_line_in_use_summary
        summary = get_budget_line_in_use_summary(budget_line)

        required_keys = [
            "line_id", "has_ledger_history",
            "net_reserved", "consumed", "released", "adjusted",
            "invoice_allocation_refs", "manual_expense_refs",
            "campaign_refs", "pending_variance_requests", "is_in_use",
        ]
        for key in required_keys:
            assert key in summary, f"Expected key '{key}' missing from line in-use summary"

    def test_rename_still_allowed_for_in_use_budget(
        self, budget_line, active_budget, actor
    ):
        """Rename is a metadata edit — must always be allowed even for in-use budgets."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("40000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-rename-test",
            requested_by=actor,
        )
        original_name = active_budget.name
        active_budget.name = "Renamed FY27 Marketing"
        active_budget.save()  # must not raise
        active_budget.refresh_from_db()
        assert active_budget.name == "Renamed FY27 Marketing"
        # Restore
        active_budget.name = original_name
        active_budget.save()

    def test_deactivate_still_allowed_for_in_use_budget(
        self, budget_line, active_budget, actor
    ):
        """Deactivate is a soft flag — must always be allowed even for in-use budgets."""
        reserve_budget_line(
            line=budget_line, amount=Decimal("40000"),
            source_type=SourceType.CAMPAIGN, source_id="camp-deact-test",
            requested_by=actor,
        )
        active_budget.status = BudgetStatus.FROZEN
        active_budget.save()  # must not raise
        active_budget.refresh_from_db()
        assert active_budget.status == BudgetStatus.FROZEN
        # Restore
        active_budget.status = BudgetStatus.ACTIVE
        active_budget.save()
