from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PeriodType(models.TextChoices):
    YEARLY = "yearly", "Yearly"
    QUARTERLY = "quarterly", "Quarterly"
    MONTHLY = "monthly", "Monthly"
    CAMPAIGN = "campaign", "Campaign"


class BudgetStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    EXHAUSTED = "exhausted", "Exhausted"
    FROZEN = "frozen", "Frozen"
    CLOSED = "closed", "Closed"


class ConsumptionType(models.TextChoices):
    RESERVED = "reserved", "Reserved"
    CONSUMED = "consumed", "Consumed"
    RELEASED = "released", "Released"
    ADJUSTED = "adjusted", "Adjusted"


class ConsumptionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPLIED = "applied", "Applied"
    REVERSED = "reversed", "Reversed"


class VarianceStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"


class SourceType(models.TextChoices):
    CAMPAIGN = "campaign", "Campaign"
    INVOICE = "invoice", "Invoice"
    MANUAL_ADJUSTMENT = "manual_adjustment", "Manual Adjustment"


# ---------------------------------------------------------------------------
# BudgetCategory
# ---------------------------------------------------------------------------

class BudgetCategory(models.Model):
    """
    Top-level budget classification within an organization.
    Examples: Marketing, Operations, IT.
    """
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="budget_categories",
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "budget_categories"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "code"],
                name="unique_category_per_org",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "is_active"]),
        ]

    def __str__(self):
        return f"{self.name} [{self.code}]"


# ---------------------------------------------------------------------------
# BudgetSubCategory
# ---------------------------------------------------------------------------

class BudgetSubCategory(models.Model):
    """
    Subdivision of a BudgetCategory.
    Examples: Digital Ads, Events, Influencer (all under Marketing).
    """
    category = models.ForeignKey(
        BudgetCategory,
        on_delete=models.CASCADE,
        related_name="subcategories",
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "budget_subcategories"
        constraints = [
            models.UniqueConstraint(
                fields=["category", "code"],
                name="unique_subcategory_per_category",
            ),
        ]
        indexes = [
            models.Index(fields=["category", "is_active"]),
        ]

    def __str__(self):
        return f"{self.category.name} > {self.name} [{self.code}]"


# ---------------------------------------------------------------------------
# Budget (header / named bucket)
# ---------------------------------------------------------------------------

class Budget(models.Model):
    """
    Named budget bucket for a scope node and financial period.
    Category/subcategory allocations live on BudgetLine children.
    """
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="budgets",
        null=True,
        blank=True,
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="budgets",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255, help_text="Human-readable name, e.g. FY27 Marketing - North")
    code = models.CharField(max_length=100, help_text="Short code, e.g. FY27-MKT-NORTH")
    financial_year = models.CharField(max_length=20, help_text="e.g. 2026-27", null=True, blank=True)
    period_type = models.CharField(
        max_length=20,
        choices=PeriodType.choices,
        default=PeriodType.YEARLY,
        null=True,
        blank=True,
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    allocated_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    reserved_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    consumed_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    currency = models.CharField(max_length=10, default="INR")
    status = models.CharField(
        max_length=20,
        choices=BudgetStatus.choices,
        default=BudgetStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_budgets",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_budgets",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "budgets"
        constraints = [
            models.UniqueConstraint(
                fields=["scope_node", "financial_year", "code"],
                name="unique_budget_per_scope_code_year",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "status"]),
            models.Index(fields=["scope_node", "status"]),
            models.Index(fields=["financial_year"]),
        ]

    def __str__(self):
        return f"Budget {self.id}: {self.name} [{self.status}]"

    @property
    def available_amount(self) -> Decimal:
        result = self.allocated_amount - self.reserved_amount - self.consumed_amount
        return max(result, Decimal("0"))

    @property
    def utilization_percent(self) -> Decimal:
        if self.allocated_amount == 0:
            return Decimal("0")
        result = ((self.reserved_amount + self.consumed_amount) / self.allocated_amount) * 100
        return min(result, Decimal("200"))


# ---------------------------------------------------------------------------
# BudgetLine
# ---------------------------------------------------------------------------

class BudgetLine(models.Model):
    """
    One category/subcategory allocation line within a Budget header.
    Reservation and consumption targets the line; the header totals are kept
    in sync as a denormalised aggregate.
    """
    budget = models.ForeignKey(
        Budget,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    category = models.ForeignKey(
        BudgetCategory,
        on_delete=models.PROTECT,
        related_name="budget_lines",
    )
    subcategory = models.ForeignKey(
        BudgetSubCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="budget_lines",
    )
    allocated_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    reserved_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    consumed_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "budget_lines"
        indexes = [
            models.Index(fields=["budget", "category"]),
        ]

    def __str__(self):
        sub = f" > {self.subcategory.name}" if self.subcategory_id else ""
        return f"BudgetLine {self.id}: {self.category.name}{sub} [{self.budget_id}]"

    @property
    def available_amount(self) -> Decimal:
        result = self.allocated_amount - self.reserved_amount - self.consumed_amount
        return max(result, Decimal("0"))

    @property
    def utilization_percent(self) -> Decimal:
        if self.allocated_amount == 0:
            return Decimal("0")
        result = ((self.reserved_amount + self.consumed_amount) / self.allocated_amount) * 100
        return min(result, Decimal("200"))


# ---------------------------------------------------------------------------
# BudgetRule
# ---------------------------------------------------------------------------

class BudgetRule(models.Model):
    """
    Threshold rules governing reservation behavior for a specific budget.
    Defaults are applied when no explicit rule exists.
    """
    budget = models.OneToOneField(
        Budget,
        on_delete=models.CASCADE,
        related_name="rule",
    )
    warning_threshold_percent = models.DecimalField(
        max_digits=5, decimal_places=2,
        default=Decimal("80.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    approval_threshold_percent = models.DecimalField(
        max_digits=5, decimal_places=2,
        default=Decimal("100.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    hard_block_threshold_percent = models.DecimalField(
        max_digits=5, decimal_places=2,
        default=Decimal("110.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    allowed_variance_percent = models.DecimalField(
        max_digits=5, decimal_places=2,
        default=Decimal("10.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    require_hod_approval_on_variance = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "budget_rules"
        ordering = ["id"]

    def __str__(self):
        return f"Rule for Budget {self.budget_id}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.warning_threshold_percent >= self.approval_threshold_percent:
            raise ValidationError(
                "warning_threshold_percent must be less than approval_threshold_percent."
            )
        if self.approval_threshold_percent > self.hard_block_threshold_percent:
            raise ValidationError(
                "approval_threshold_percent must be <= hard_block_threshold_percent."
            )


# ---------------------------------------------------------------------------
# BudgetConsumption
# ---------------------------------------------------------------------------

class BudgetConsumption(models.Model):
    """
    Ledger entry tracking each reservation, consumption, release, or adjustment
    against a budget line (and its parent budget header).
    """
    budget = models.ForeignKey(
        Budget,
        on_delete=models.CASCADE,
        related_name="consumptions",
    )
    budget_line = models.ForeignKey(
        BudgetLine,
        on_delete=models.CASCADE,
        related_name="consumptions",
        null=True,
        blank=True,
    )
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
    )
    source_id = models.CharField(
        max_length=100,
        help_text="ID of the source record (campaign, invoice, etc.)",
    )
    amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    consumption_type = models.CharField(
        max_length=20,
        choices=ConsumptionType.choices,
    )
    status = models.CharField(
        max_length=20,
        choices=ConsumptionStatus.choices,
        default=ConsumptionStatus.APPLIED,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="budget_consumptions",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "budget_consumptions"
        indexes = [
            models.Index(fields=["budget", "source_type", "source_id"]),
            models.Index(fields=["budget_line", "source_type", "source_id"]),
            models.Index(fields=["source_type", "source_id"]),
        ]

    def __str__(self):
        return (
            f"Consumption {self.id}: {self.consumption_type} "
            f"{self.amount} [{self.source_type}:{self.source_id}]"
        )


# ---------------------------------------------------------------------------
# BudgetVarianceRequest
# ---------------------------------------------------------------------------

class BudgetVarianceRequest(models.Model):
    """
    Records a request to exceed a budget line's approval threshold.
    Created automatically by reserve_budget_line() when projected utilization
    crosses the approval threshold.
    """
    budget = models.ForeignKey(
        Budget,
        on_delete=models.CASCADE,
        related_name="variance_requests",
    )
    budget_line = models.ForeignKey(
        BudgetLine,
        on_delete=models.CASCADE,
        related_name="variance_requests",
        null=True,
        blank=True,
    )
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
    )
    source_id = models.CharField(max_length=100)
    requested_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    current_utilization_percent = models.DecimalField(max_digits=6, decimal_places=2)
    projected_utilization_percent = models.DecimalField(max_digits=6, decimal_places=2)
    reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=VarianceStatus.choices,
        default=VarianceStatus.PENDING,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="budget_variance_requests",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="budget_variance_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "budget_variance_requests"
        indexes = [
            models.Index(fields=["budget", "status"]),
            models.Index(fields=["budget_line", "status"]),
            models.Index(fields=["source_type", "source_id", "status"]),
        ]

    def __str__(self):
        return (
            f"VarianceRequest {self.id}: {self.status} "
            f"[{self.source_type}:{self.source_id}] {self.requested_amount}"
        )
