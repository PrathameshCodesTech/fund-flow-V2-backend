from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ExpenseStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    SETTLED = "settled", "Settled"
    CANCELLED = "cancelled", "Cancelled"


class PaymentMethod(models.TextChoices):
    PETTY_CASH = "petty_cash", "Petty Cash"
    REIMBURSEMENT = "reimbursement", "Reimbursement"


# ---------------------------------------------------------------------------
# ManualExpenseEntry
# ---------------------------------------------------------------------------

class ManualExpenseEntry(models.Model):
    """
    Lightweight internal expense register — no approval workflow.
    Draft → Submitted → Settled/Cancelled.
    """

    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.PROTECT,
        related_name="manual_expenses",
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="manual_expenses",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_manual_expenses",
    )
    status = models.CharField(
        max_length=20,
        choices=ExpenseStatus.choices,
        default=ExpenseStatus.DRAFT,
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
    )
    vendor_name = models.CharField(max_length=255, blank=True, default="")
    vendor = models.ForeignKey(
        "vendors.Vendor",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="manual_expenses",
    )
    reference_number = models.CharField(max_length=255, blank=True, default="")
    expense_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    budget = models.ForeignKey(
        "budgets.Budget",
        on_delete=models.PROTECT,
        related_name="manual_expenses",
    )
    budget_line = models.ForeignKey(
        "budgets.BudgetLine",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="manual_expenses",
    )
    category = models.ForeignKey(
        "budgets.BudgetCategory",
        on_delete=models.PROTECT,
        related_name="manual_expenses",
    )
    subcategory = models.ForeignKey(
        "budgets.BudgetSubCategory",
        on_delete=models.PROTECT,
        related_name="manual_expenses",
    )
    description = models.TextField(blank=True, default="")
    source_note = models.TextField(blank=True, default="")
    submitted_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "manual_expense_entries"
        indexes = [
            models.Index(fields=["org", "status"]),
            models.Index(fields=["org", "scope_node"]),
            models.Index(fields=["created_by"]),
            models.Index(fields=["expense_date"]),
            models.Index(fields=["budget", "category", "subcategory"]),
        ]

    def __str__(self):
        return f"{self.vendor_name or 'Unknown vendor'} — {self.amount} ({self.expense_date})"

    @property
    def attachment_count(self) -> int:
        return self.attachments.count()


# ---------------------------------------------------------------------------
# ManualExpenseAttachment
# ---------------------------------------------------------------------------

class ManualExpenseAttachment(models.Model):
    expense_entry = models.ForeignKey(
        ManualExpenseEntry,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="manual_expense_attachments/")
    title = models.CharField(max_length=255)
    document_type = models.CharField(max_length=100, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="manual_expense_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "manual_expense_attachments"
        indexes = [
            models.Index(fields=["expense_entry"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.document_type})"