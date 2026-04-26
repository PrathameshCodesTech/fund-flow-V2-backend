from django.utils import timezone
from apps.manual_expenses.models import ManualExpenseEntry, ManualExpenseAttachment, ExpenseStatus


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

def mark_expense_settled(expense: ManualExpenseEntry) -> ManualExpenseEntry:
    """Move expense from SUBMITTED to SETTLED."""
    if expense.status != ExpenseStatus.SUBMITTED:
        raise ExpenseValidationError(
            f"Cannot settle expense in status '{expense.status}'. Only SUBMITTED expenses can be settled."
        )
    expense.status = ExpenseStatus.SETTLED
    expense.settled_at = _now()
    expense.save()
    return expense


def cancel_expense(expense: ManualExpenseEntry) -> ManualExpenseEntry:
    """
    Move expense from DRAFT or SUBMITTED to CANCELLED.
    Cannot cancel SETTLED expenses.
    """
    if expense.status == ExpenseStatus.SETTLED:
        raise ExpenseValidationError("Cannot cancel a SETTLED expense.")
    expense.status = ExpenseStatus.CANCELLED
    expense.cancelled_at = _now()
    expense.save()
    return expense


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