"""
Generic Finance Handoff Layer.

Represents the bridge between internal workflow approval and external finance review.

Supported modules:
    - invoice
    - campaign
    - vendor (future: convergence path)
    - budget (future-safe)
"""

import secrets
from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FinanceHandoffStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent to Finance"
    FINANCE_APPROVED = "finance_approved", "Finance Approved"
    FINANCE_REJECTED = "finance_rejected", "Finance Rejected"
    CANCELLED = "cancelled", "Cancelled"


class FinanceActionType(models.TextChoices):
    APPROVE = "approve", "Approve"
    REJECT = "reject", "Reject"


class FinanceDecisionChoice(models.TextChoices):
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


# ---------------------------------------------------------------------------
# FinanceHandoff
# ---------------------------------------------------------------------------

class FinanceHandoff(models.Model):
    """
    Represents one business object sent to external finance for review.

    There can be many historical handoff records for the same subject,
    but only one active (pending or sent) handoff is allowed per subject at a time.

    module: the broad category (invoice, campaign, vendor, budget)
    subject_type: the specific type within that module
    subject_id: the primary key of the subject record
    """
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="finance_handoffs",
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="finance_handoffs",
    )
    module = models.CharField(
        max_length=20,
        help_text="invoice | campaign | vendor | budget",
    )
    subject_type = models.CharField(
        max_length=30,
        help_text="e.g. vendor_submission, invoice, campaign",
    )
    subject_id = models.PositiveBigIntegerField(
        help_text="PK of the subject record",
    )
    status = models.CharField(
        max_length=25,
        choices=FinanceHandoffStatus.choices,
        default=FinanceHandoffStatus.PENDING,
    )
    export_file = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path or reference to the generated export file",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="finance_handoffs_triggered",
    )
    finance_reference_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="External reference assigned by finance (e.g. SAP vendor code, invoice number)",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_handoffs"
        indexes = [
            models.Index(fields=["module", "subject_type", "subject_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["org", "scope_node"]),
            models.Index(fields=["finance_reference_id"]),
        ]

    def __str__(self):
        return (
            f"FinanceHandoff {self.id}: {self.module}/{self.subject_type}:{self.subject_id} "
            f"[{self.status}]"
        )

    def is_active(self):
        return self.status in (
            FinanceHandoffStatus.PENDING,
            FinanceHandoffStatus.SENT,
        )


# ---------------------------------------------------------------------------
# FinanceActionToken
# ---------------------------------------------------------------------------

class FinanceActionToken(models.Model):
    """
    A short-lived token that grants an external finance user permission to
    approve or reject a FinanceHandoff without authenticating into the platform.

    Tokens are single-use; once used they cannot be reused.
    Tokens expire at a configurable time (default: 72 hours from creation).
    """
    handoff = models.ForeignKey(
        FinanceHandoff,
        on_delete=models.CASCADE,
        related_name="action_tokens",
    )
    action_type = models.CharField(
        max_length=10,
        choices=FinanceActionType.choices,
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_action_tokens"
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["handoff", "action_type"]),
        ]

    def __str__(self):
        return f"FinanceActionToken {self.id}: {self.action_type} [{'used' if self.is_used() else 'active'}]"

    def is_used(self):
        return self.used_at is not None

    def is_expired(self):
        return timezone.now() > self.expires_at


# ---------------------------------------------------------------------------
# FinanceDecision
# ---------------------------------------------------------------------------

class FinanceDecision(models.Model):
    """
    Records an approve or reject decision made by an external finance user
    via a FinanceActionToken.

    One decision per token (tokens are single-use).
    """
    handoff = models.ForeignKey(
        FinanceHandoff,
        on_delete=models.CASCADE,
        related_name="decisions",
    )
    decision = models.CharField(
        max_length=10,
        choices=FinanceDecisionChoice.choices,
    )
    reference_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Finance-assigned reference (e.g. SAP vendor code, PO number)",
    )
    note = models.TextField(blank=True)
    acted_via_token = models.ForeignKey(
        FinanceActionToken,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decisions",
    )
    acted_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_decisions"
        ordering = ["-acted_at"]
        indexes = [
            models.Index(fields=["handoff", "decision"]),
        ]

    def __str__(self):
        return f"FinanceDecision {self.id}: {self.decision} [{self.acted_at}]"
