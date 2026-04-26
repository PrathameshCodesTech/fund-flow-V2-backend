import pytest
from datetime import date
from decimal import Decimal
from django.contrib.auth import get_user_model

from apps.core.models import Organization, ScopeNode, NodeType
from apps.budgets.models import Budget, BudgetCategory, BudgetSubCategory, BudgetStatus
from apps.manual_expenses.models import ManualExpenseEntry, ManualExpenseAttachment, ExpenseStatus, PaymentMethod
from apps.manual_expenses.services import (
    create_expense_draft,
    save_expense_draft,
    submit_expense,
    mark_expense_settled,
    cancel_expense,
    ExpenseValidationError,
)


User = get_user_model()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Acme Corp", code="acme")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/acme/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Marketing", code="mkt",
        node_type=NodeType.ENTITY, path="/acme/hq/mkt", depth=1,
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="finance@acme.com", password="pass")


@pytest.fixture
def budget(org, entity):
    cat = BudgetCategory.objects.create(org=org, name="Travel", code="TRV")
    sub = BudgetSubCategory.objects.create(category=cat, name="Flights", code="FLT")
    b = Budget.objects.create(
        org=org,
        scope_node=entity,
        name="Q1 Travel Budget",
        code="Q1-TRV",
        period_type="quarterly",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 3, 31),
        allocated_amount=Decimal("50000.00"),
        status=BudgetStatus.ACTIVE,
    )
    return b


@pytest.fixture
def category(org):
    return BudgetCategory.objects.create(org=org, name="Office Supplies", code="OFF")


@pytest.fixture
def subcategory(category):
    return BudgetSubCategory.objects.create(category=category, name="Stationery", code="STA")


@pytest.fixture
def expense_data(org, entity, user, budget, category, subcategory):
    return {
        "payment_method": PaymentMethod.PETTY_CASH,
        "vendor_name": "Amazon India",
        "reference_number": "INV-2025-001",
        "expense_date": date(2025, 2, 10),
        "amount": Decimal("1500.00"),
        "currency": "INR",
        "budget": budget,
        "category": category,
        "subcategory": subcategory,
        "description": "Office stationery purchase",
        "source_note": "Bills submitted via email",
    }


# ---------------------------------------------------------------------------
# Draft operations
# ---------------------------------------------------------------------------

class TestCreateExpenseDraft:
    def test_creates_draft_expense(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        assert expense.id is not None
        assert expense.status == ExpenseStatus.DRAFT
        assert expense.vendor_name == "Amazon India"
        assert expense.amount == Decimal("1500.00")
        assert expense.created_by == user

    def test_status_defaults_to_draft(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        assert expense.status == ExpenseStatus.DRAFT


class TestSaveExpenseDraft:
    def test_updates_draft_fields(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        updated = save_expense_draft(expense, {"vendor_name": "Updated Vendor", "amount": Decimal("2000.00")})
        assert updated.vendor_name == "Updated Vendor"
        assert updated.amount == Decimal("2000.00")

    def test_raises_if_not_draft(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        expense.status = ExpenseStatus.SUBMITTED
        expense.save()
        with pytest.raises(ExpenseValidationError):
            save_expense_draft(expense, {"vendor_name": "Hack"})


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

class TestSubmitExpense:
    def test_transitions_draft_to_submitted(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        # Add an attachment (required for submit)
        ManualExpenseAttachment.objects.create(
            expense_entry=expense,
            title="Receipt",
            document_type="receipt",
            uploaded_by=user,
        )
        result = submit_expense(expense)
        assert result.status == ExpenseStatus.SUBMITTED
        assert result.submitted_at is not None

    def test_raises_if_missing_attachment(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        with pytest.raises(ExpenseValidationError):
            submit_expense(expense)

    def test_raises_if_amount_zero(self, org, entity, user, expense_data):
        expense_data["amount"] = Decimal("0.00")
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        ManualExpenseAttachment.objects.create(
            expense_entry=expense, title="x", document_type="x", uploaded_by=user,
        )
        with pytest.raises(ExpenseValidationError):
            submit_expense(expense)

    def test_raises_if_not_draft(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        expense.status = ExpenseStatus.SETTLED
        expense.save()
        with pytest.raises(ExpenseValidationError):
            submit_expense(expense)


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------

class TestMarkExpenseSettled:
    def test_transitions_submitted_to_settled(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        ManualExpenseAttachment.objects.create(
            expense_entry=expense, title="x", document_type="x", uploaded_by=user,
        )
        expense = submit_expense(expense)
        result = mark_expense_settled(expense)
        assert result.status == ExpenseStatus.SETTLED
        assert result.settled_at is not None

    def test_raises_if_not_submitted(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        with pytest.raises(ExpenseValidationError):
            mark_expense_settled(expense)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

class TestCancelExpense:
    def test_cancels_draft_expense(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        result = cancel_expense(expense)
        assert result.status == ExpenseStatus.CANCELLED
        assert result.cancelled_at is not None

    def test_cancels_submitted_expense(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        ManualExpenseAttachment.objects.create(
            expense_entry=expense, title="x", document_type="x", uploaded_by=user,
        )
        expense = submit_expense(expense)
        result = cancel_expense(expense)
        assert result.status == ExpenseStatus.CANCELLED

    def test_cannot_cancel_settled(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        ManualExpenseAttachment.objects.create(
            expense_entry=expense, title="x", document_type="x", uploaded_by=user,
        )
        expense = submit_expense(expense)
        expense = mark_expense_settled(expense)
        with pytest.raises(ExpenseValidationError) as exc_info:
            cancel_expense(expense)
        assert "SETTLED" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Attachment count property
# ---------------------------------------------------------------------------

class TestAttachmentCount:
    def test_returns_zero_when_no_attachments(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        assert expense.attachment_count == 0

    def test_returns_correct_count(self, org, entity, user, expense_data):
        expense = create_expense_draft(
            org=org, scope_node=entity, created_by=user, data=expense_data,
        )
        ManualExpenseAttachment.objects.create(
            expense_entry=expense, title="Receipt 1", document_type="receipt", uploaded_by=user,
        )
        ManualExpenseAttachment.objects.create(
            expense_entry=expense, title="Receipt 2", document_type="receipt", uploaded_by=user,
        )
        assert expense.attachment_count == 2