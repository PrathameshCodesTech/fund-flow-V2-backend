from django.utils import timezone
from apps.manual_expenses.models import ManualExpenseEntry, ManualExpenseAttachment, ExpenseStatus
from apps.budgets.models import SourceType


class ExpenseValidationError(ValueError):
    """Raised when expense validation fails."""
    pass


def _now():
    return timezone.now()


# ---------------------------------------------------------------------------
# Draft operations
# ---------------------------------------------------------------------------

def create_expense_draft(*, org, scope_node, created_by, data) -> ManualExpenseEntry:
    """Create a new expense in DRAFT status."""
    return ManualExpenseEntry.objects.create(
        org=org,
        scope_node=scope_node,
        created_by=created_by,
        status=ExpenseStatus.DRAFT,
        **data,
    )


def save_expense_draft(expense: ManualExpenseEntry, data) -> ManualExpenseEntry:
    """
    Update a draft expense. Only allowed while status is DRAFT.
    The expense must belong to request.user (enforced at view level).
    """
    if expense.status != ExpenseStatus.DRAFT:
        raise ExpenseValidationError(
            f"Cannot edit expense in status '{expense.status}'. Only DRAFT expenses can be edited."
        )
    for key, value in data.items():
        setattr(expense, key, value)
    expense.save()
    return expense


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

def _validate_submit(expense: ManualExpenseEntry):
    """
    Validate submission requirements:
    - expense_date, amount, budget, category, subcategory, payment_method must be set
    - amount must be > 0
    - at least one attachment
    """
    errors = {}

    if not expense.expense_date:
        errors.setdefault("expense_date", []).append("Expense date is required.")
    if not expense.amount or expense.amount <= 0:
        errors.setdefault("amount", []).append("Amount must be greater than zero.")
    if not expense.budget_id:
        errors.setdefault("budget", []).append("Budget is required.")
    if not expense.category_id:
        errors.setdefault("category", []).append("Category is required.")
    if not expense.subcategory_id:
        errors.setdefault("subcategory", []).append("Subcategory is required.")
    if not expense.payment_method:
        errors.setdefault("payment_method", []).append("Payment method is required.")
    if expense.attachments.count() == 0:
        errors.setdefault("attachments", []).append("At least one attachment is required to submit.")

    if errors:
        raise ExpenseValidationError(errors)

    return True


def submit_expense(expense: ManualExpenseEntry) -> ManualExpenseEntry:
    """
    Move expense from DRAFT to SUBMITTED.
    Enforces validation rules before transitioning.
    """
    if expense.status != ExpenseStatus.DRAFT:
        raise ExpenseValidationError(
            f"Cannot submit expense in status '{expense.status}'. Only DRAFT expenses can be submitted."
        )
    _validate_submit(expense)
    expense.status = ExpenseStatus.SUBMITTED
    expense.submitted_at = _now()
    expense.save()
    return expense


# ---------------------------------------------------------------------------
# Settle / Cancel (privileged actors — enforced at view level)
# ---------------------------------------------------------------------------

def mark_expense_settled(expense: ManualExpenseEntry, settled_by=None) -> ManualExpenseEntry:
    """
    Move expense from SUBMITTED to SETTLED.
    Records a CONSUMED BudgetConsumption entry against the expense's budget line.
    """
    if expense.status != ExpenseStatus.SUBMITTED:
        raise ExpenseValidationError(
            f"Cannot settle expense in status '{expense.status}'. Only SUBMITTED expenses can be settled."
        )

    expense.status = ExpenseStatus.SETTLED
    expense.settled_at = _now()
    expense.save()

    # Record consumption against the budget ledger
    if expense.budget_id and expense.amount and expense.amount > 0:
        _consume_expense_budget(expense, settled_by or expense.created_by)

    return expense


def _consume_expense_budget(expense: ManualExpenseEntry, actor) -> None:
    """Record a CONSUMED ledger entry for a settled manual expense."""
    from apps.budgets.services import (
        consume_reserved_budget_line,
        get_source_reserved_balance_for_line,
        BudgetStatus,
    )
    from apps.budgets.models import (
        BudgetConsumption, ConsumptionType, ConsumptionStatus, Budget
    )

    source_type = SourceType.MANUAL_EXPENSE
    source_id = str(expense.id)

    budget_line = expense.budget_line
    budget = expense.budget

    # If no reserved balance exists for this expense (expense was never reserved),
    # create a direct CONSUMED entry rather than going through consume_reserved.
    if budget_line and budget.status in (BudgetStatus.ACTIVE, BudgetStatus.EXHAUSTED):
        reserved_balance = get_source_reserved_balance_for_line(budget_line, source_type, source_id)
        if reserved_balance > 0:
            consume_amount = min(reserved_balance, expense.amount)
            consume_reserved_budget_line(
                line=budget_line,
                amount=consume_amount,
                source_type=source_type,
                source_id=source_id,
                consumed_by=actor,
                note=f"Manual expense #{expense.id} settled",
            )
            return

    # No prior reservation — direct consume entry
    if budget.status in (BudgetStatus.ACTIVE, BudgetStatus.EXHAUSTED):
        BudgetConsumption.objects.create(
            budget=budget,
            budget_line=budget_line,
            source_type=source_type,
            source_id=source_id,
            amount=expense.amount,
            consumption_type=ConsumptionType.CONSUMED,
            status=ConsumptionStatus.APPLIED,
            created_by=actor,
            note=f"Manual expense #{expense.id} settled (direct consume)",
        )
        budget.refresh_from_db()
        budget.consumed_amount = (budget.consumed_amount or 0) + expense.amount
        budget.save(update_fields=["consumed_amount", "updated_at"])
        if budget_line:
            budget_line.refresh_from_db()
            budget_line.consumed_amount = (budget_line.consumed_amount or 0) + expense.amount
            budget_line.save(update_fields=["consumed_amount", "updated_at"])


def cancel_expense(expense: ManualExpenseEntry, cancelled_by=None) -> ManualExpenseEntry:
    """
    Move expense from DRAFT or SUBMITTED to CANCELLED.
    If SUBMITTED, releases any reserved budget associated with this expense.
    Cannot cancel SETTLED expenses.
    """
    if expense.status == ExpenseStatus.SETTLED:
        raise ExpenseValidationError("Cannot cancel a SETTLED expense.")

    was_submitted = expense.status == ExpenseStatus.SUBMITTED
    expense.status = ExpenseStatus.CANCELLED
    expense.cancelled_at = _now()
    expense.save()

    if was_submitted and expense.budget_id and expense.amount and expense.amount > 0:
        _release_expense_budget(expense, cancelled_by or expense.created_by)

    return expense


def _release_expense_budget(expense: ManualExpenseEntry, actor) -> None:
    """Release any reserved budget when a submitted expense is cancelled."""
    from apps.budgets.services import (
        release_reserved_budget_line,
        get_source_reserved_balance_for_line,
        release_reserved_budget,
        get_source_reserved_balance,
        BudgetStatus,
    )

    source_type = SourceType.MANUAL_EXPENSE
    source_id = str(expense.id)
    budget = expense.budget
    budget_line = expense.budget_line

    if budget.status not in (BudgetStatus.ACTIVE, BudgetStatus.EXHAUSTED):
        return

    if budget_line:
        balance = get_source_reserved_balance_for_line(budget_line, source_type, source_id)
        if balance > 0:
            release_reserved_budget_line(
                line=budget_line,
                amount=balance,
                source_type=source_type,
                source_id=source_id,
                released_by=actor,
                note=f"Manual expense #{expense.id} cancelled",
            )
    else:
        balance = get_source_reserved_balance(budget, source_type, source_id)
        if balance > 0:
            release_reserved_budget(
                budget=budget,
                amount=balance,
                source_type=source_type,
                source_id=source_id,
                released_by=actor,
                note=f"Manual expense #{expense.id} cancelled",
            )


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def upload_expense_attachment(
    expense: ManualExpenseEntry,
    file,
    title: str,
    document_type: str,
    uploaded_by,
) -> ManualExpenseAttachment:
    """Attach a file to an expense. Allowed on DRAFT or SUBMITTED expenses."""
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.SUBMITTED):
        raise ExpenseValidationError(
            f"Cannot attach files to expense in status '{expense.status}'."
        )
    return ManualExpenseAttachment.objects.create(
        expense_entry=expense,
        file=file,
        title=title,
        document_type=document_type,
        uploaded_by=uploaded_by,
    )


def delete_expense_attachment(attachment: ManualExpenseAttachment) -> None:
    """Delete an attachment. Allowed on DRAFT or SUBMITTED expenses."""
    expense = attachment.expense_entry
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.SUBMITTED):
        raise ExpenseValidationError(
            f"Cannot delete attachments on expense in status '{expense.status}'."
        )
    attachment.delete()