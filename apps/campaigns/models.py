from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CampaignStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_BUDGET = "pending_budget", "Pending Budget"
    BUDGET_VARIANCE_PENDING = "budget_variance_pending", "Budget Variance Pending"
    PENDING_WORKFLOW = "pending_workflow", "Pending Workflow"
    IN_REVIEW = "in_review", "In Review"
    INTERNALLY_APPROVED = "internally_approved", "Internally Approved"
    FINANCE_PENDING = "finance_pending", "Finance Pending"
    FINANCE_APPROVED = "finance_approved", "Finance Approved"
    FINANCE_REJECTED = "finance_rejected", "Finance Rejected"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

class Campaign(models.Model):
    """
    A spend-request / planning record that optionally consumes budget
    and can be routed through workflow approval.
    """
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="campaigns",
        null=True,
        blank=True,
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="campaigns",
        help_text="Entity or company this campaign belongs to",
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    campaign_type = models.CharField(max_length=100, blank=True, default="")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    requested_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    approved_amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    currency = models.CharField(max_length=10, default="INR")
    category = models.ForeignKey(
        "budgets.BudgetCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns",
    )
    subcategory = models.ForeignKey(
        "budgets.BudgetSubCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns",
    )
    budget = models.ForeignKey(
        "budgets.Budget",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns",
    )
    budget_variance_request = models.ForeignKey(
        "budgets.BudgetVarianceRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_campaigns",
    )
    status = models.CharField(
        max_length=30,
        choices=CampaignStatus.choices,
        default=CampaignStatus.DRAFT,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "campaigns"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["org", "status"]),
            models.Index(fields=["scope_node", "status"]),
            models.Index(fields=["category"]),
            models.Index(fields=["budget"]),
        ]

    def __str__(self):
        return f"Campaign {self.id}: {self.name} [{self.status}]"


# ---------------------------------------------------------------------------
# CampaignDocument
# ---------------------------------------------------------------------------

class CampaignDocument(models.Model):
    """
    Placeholder for uploaded documents / scan metadata linked to a campaign.
    Real file storage is not implemented in V1.
    """
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    title = models.CharField(max_length=255)
    file_url = models.CharField(max_length=500)
    document_type = models.CharField(max_length=100, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaign_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "campaign_documents"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Document {self.id}: {self.title} [campaign {self.campaign_id}]"
