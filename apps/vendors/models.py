import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Choice enums
# ---------------------------------------------------------------------------

class InvitationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    OPENED = "opened", "Opened"
    SUBMITTED = "submitted", "Submitted"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"


class SubmissionMode(models.TextChoices):
    MANUAL = "manual", "Manual"
    EXCEL_UPLOAD = "excel_upload", "Excel Upload"


class SubmissionStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    SENT_TO_FINANCE = "sent_to_finance", "Sent to Finance"
    FINANCE_APPROVED = "finance_approved", "Finance Approved"
    FINANCE_REJECTED = "finance_rejected", "Finance Rejected"
    REOPENED = "reopened", "Reopened"
    MARKETING_PENDING = "marketing_pending", "Marketing Pending"
    MARKETING_APPROVED = "marketing_approved", "Marketing Approved"
    ACTIVATED = "activated", "Activated"
    REJECTED = "rejected", "Rejected"


class FinanceActionType(models.TextChoices):
    APPROVE = "approve", "Approve"
    REJECT = "reject", "Reject"


class FinanceDecisionChoice(models.TextChoices):
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class MarketingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class OperationalStatus(models.TextChoices):
    INACTIVE = "inactive", "Inactive"
    WAITING_MARKETING_APPROVAL = "waiting_marketing_approval", "Waiting Marketing Approval"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"


class VendorProfileRevisionStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    FINANCE_APPROVED = "finance_approved", "Finance Approved"
    FINANCE_REJECTED = "finance_rejected", "Finance Rejected"
    REOPENED = "reopened", "Reopened"
    APPLIED = "applied", "Applied"
    CANCELLED = "cancelled", "Cancelled"


# ---------------------------------------------------------------------------
# Attachment document type constants
# ---------------------------------------------------------------------------

#: Allowed document_type values for VendorAttachment on the vendor portal.
ALLOWED_ATTACHMENT_DOCUMENT_TYPES: set[str] = {
    "msme_declaration_form",
    "msme_registration_certificate",
    "cancelled_cheque",
    "pan_copy",
    "gst_certificate",
    "bank_proof",
    "supporting_document",
    # Legacy/generic types already stored in production
    "kyc_proof",
    "address_proof",
    "gst",
    "pan",
    "other",
}


#: Allowed msme_enterprise_type values.
ALLOWED_MSME_ENTERPRISE_TYPES: set[str] = {"micro", "small", "medium"}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class VendorInvitation(models.Model):
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.PROTECT,
        related_name="vendor_invitations",
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="vendor_invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_vendor_invitations",
    )
    vendor_email = models.EmailField()
    vendor_name_hint = models.CharField(max_length=255, blank=True)
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vendor_invitations"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["org", "status"]),
            models.Index(fields=["vendor_email"]),
        ]

    def __str__(self):
        return f"VendorInvitation {self.id}: {self.vendor_email} [{self.status}]"

    def is_expired(self):
        if self.expires_at and timezone.now() > self.expires_at:
            return True
        return False


class VendorOnboardingSubmission(models.Model):
    invitation = models.ForeignKey(
        VendorInvitation,
        on_delete=models.PROTECT,
        related_name="submissions",
    )
    submission_mode = models.CharField(
        max_length=20,
        choices=SubmissionMode.choices,
        default=SubmissionMode.MANUAL,
    )
    status = models.CharField(
        max_length=30,
        choices=SubmissionStatus.choices,
        default=SubmissionStatus.DRAFT,
    )
    raw_form_data = models.JSONField(default=dict, blank=True)

    # Normalized core fields
    normalized_title = models.CharField(max_length=100, blank=True)
    normalized_vendor_name = models.CharField(max_length=255, blank=True)
    normalized_vendor_type = models.CharField(max_length=100, blank=True)
    normalized_email = models.EmailField(blank=True)
    normalized_phone = models.CharField(max_length=50, blank=True)
    normalized_fax = models.CharField(max_length=50, blank=True)
    normalized_gst_registered = models.BooleanField(null=True, blank=True)
    normalized_gstin = models.CharField(max_length=20, blank=True)
    normalized_pan = models.CharField(max_length=20, blank=True)
    normalized_region = models.CharField(max_length=100, blank=True)
    normalized_head_office_no = models.CharField(max_length=50, blank=True)

    # Address
    normalized_address_line1 = models.CharField(max_length=255, blank=True)
    normalized_address_line2 = models.CharField(max_length=255, blank=True)
    normalized_address_line3 = models.CharField(max_length=255, blank=True)
    normalized_city = models.CharField(max_length=100, blank=True)
    normalized_state = models.CharField(max_length=100, blank=True)
    normalized_country = models.CharField(max_length=100, blank=True)
    normalized_pincode = models.CharField(max_length=20, blank=True)

    # Bank — core
    normalized_preferred_payment_mode = models.CharField(max_length=100, blank=True)
    normalized_beneficiary_name = models.CharField(max_length=255, blank=True)
    normalized_bank_name = models.CharField(max_length=255, blank=True)
    normalized_account_number = models.CharField(max_length=50, blank=True)
    normalized_bank_account_type = models.CharField(max_length=100, blank=True)
    normalized_ifsc = models.CharField(max_length=20, blank=True)
    normalized_micr_code = models.CharField(max_length=20, blank=True)
    normalized_neft_code = models.CharField(max_length=50, blank=True)

    # Bank — branch contact
    normalized_bank_branch_address_line1 = models.CharField(max_length=255, blank=True)
    normalized_bank_branch_address_line2 = models.CharField(max_length=255, blank=True)
    normalized_bank_branch_city = models.CharField(max_length=100, blank=True)
    normalized_bank_branch_state = models.CharField(max_length=100, blank=True)
    normalized_bank_branch_country = models.CharField(max_length=100, blank=True)
    normalized_bank_branch_pincode = models.CharField(max_length=20, blank=True)
    normalized_bank_phone = models.CharField(max_length=50, blank=True)
    normalized_bank_fax = models.CharField(max_length=50, blank=True)

    # MSME / compliance
    normalized_authorized_signatory_name = models.CharField(max_length=255, blank=True)
    normalized_msme_registered = models.BooleanField(null=True, blank=True)
    normalized_msme_registration_number = models.CharField(max_length=100, blank=True)
    normalized_msme_enterprise_type = models.CharField(max_length=50, blank=True)
    declaration_accepted = models.BooleanField(null=True, blank=True)

    # Structured JSON blocks (secondary data, display/export only)
    contact_persons_json = models.JSONField(default=list, blank=True)
    head_office_address_json = models.JSONField(default=dict, blank=True)
    tax_registration_details_json = models.JSONField(default=dict, blank=True)

    # File references (paths)
    source_excel_file = models.CharField(max_length=500, blank=True)
    exported_excel_file = models.CharField(max_length=500, blank=True)

    # Finance tracking
    finance_sent_at = models.DateTimeField(null=True, blank=True)
    finance_vendor_code = models.CharField(max_length=100, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vendor_onboarding_submissions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["invitation", "status"]),
            models.Index(fields=["status"]),
            models.Index(fields=["normalized_email"]),
        ]

    def __str__(self):
        return f"Submission {self.id}: {self.normalized_vendor_name} [{self.status}]"


def _vendor_attachment_upload_path(instance, filename):
    sub_id = getattr(instance, "submission_id", None) or "unknown"
    return f"vendor_attachments/sub_{sub_id}/{filename}"


class VendorAttachment(models.Model):
    submission = models.ForeignKey(
        VendorOnboardingSubmission,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    document_type = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=255)
    file_name = models.CharField(max_length=500, blank=True)
    file = models.FileField(
        upload_to=_vendor_attachment_upload_path,
        blank=True,
    )
    file_url = models.CharField(max_length=1000, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vendor_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "vendor_attachments"
        ordering = ["-created_at"]

    def __str__(self):
        return f"VendorAttachment {self.id}: {self.title}"


class VendorFinanceActionToken(models.Model):
    submission = models.ForeignKey(
        VendorOnboardingSubmission,
        on_delete=models.CASCADE,
        related_name="finance_tokens",
    )
    action_type = models.CharField(max_length=10, choices=FinanceActionType.choices)
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "vendor_finance_action_tokens"
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["submission", "action_type"]),
        ]

    def __str__(self):
        return f"FinanceToken {self.id}: {self.action_type} for submission {self.submission_id}"

    def is_expired(self):
        return timezone.now() > self.expires_at

    def is_used(self):
        return self.used_at is not None


class VendorFinanceDecision(models.Model):
    submission = models.ForeignKey(
        VendorOnboardingSubmission,
        on_delete=models.CASCADE,
        related_name="finance_decisions",
    )
    decision = models.CharField(max_length=10, choices=FinanceDecisionChoice.choices)
    sap_vendor_id = models.CharField(max_length=100, blank=True)
    note = models.TextField(blank=True)
    acted_via_token = models.ForeignKey(
        VendorFinanceActionToken,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decisions",
    )
    acted_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "vendor_finance_decisions"
        ordering = ["-acted_at"]

    def __str__(self):
        return f"FinanceDecision {self.id}: {self.decision} for submission {self.submission_id}"


class Vendor(models.Model):
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.PROTECT,
        related_name="vendors",
        null=True,
        blank=True,
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="vendors",
    )
    onboarding_submission = models.OneToOneField(
        VendorOnboardingSubmission,
        on_delete=models.PROTECT,
        related_name="vendor",
        null=True,
        blank=True,
    )
    vendor_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)

    # ── Approved live profile fields ───────────────────────────────────────────
    # Authoritative approved vendor profile. OnboardingSubmission is the
    # source for onboarding-time data; Vendor holds the current approved state.
    # build_vendor_live_snapshot() and apply_vendor_profile_revision() use these.
    title = models.CharField(max_length=100, blank=True)
    vendor_type = models.CharField(max_length=100, blank=True)
    fax = models.CharField(max_length=50, blank=True)
    region = models.CharField(max_length=100, blank=True)
    head_office_no = models.CharField(max_length=50, blank=True)
    gst_registered = models.BooleanField(null=True, blank=True)
    gstin = models.CharField(max_length=20, blank=True)
    pan = models.CharField(max_length=20, blank=True)
    # Address
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    address_line3 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=20, blank=True)
    # Bank core
    preferred_payment_mode = models.CharField(max_length=100, blank=True)
    beneficiary_name = models.CharField(max_length=255, blank=True)
    bank_name = models.CharField(max_length=255, blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    bank_account_type = models.CharField(max_length=100, blank=True)
    ifsc = models.CharField(max_length=20, blank=True)
    micr_code = models.CharField(max_length=20, blank=True)
    neft_code = models.CharField(max_length=50, blank=True)
    # Bank branch contact
    bank_branch_address_line1 = models.CharField(max_length=255, blank=True)
    bank_branch_address_line2 = models.CharField(max_length=255, blank=True)
    bank_branch_city = models.CharField(max_length=100, blank=True)
    bank_branch_state = models.CharField(max_length=100, blank=True)
    bank_branch_country = models.CharField(max_length=100, blank=True)
    bank_branch_pincode = models.CharField(max_length=20, blank=True)
    bank_phone = models.CharField(max_length=50, blank=True)
    bank_fax = models.CharField(max_length=50, blank=True)
    # MSME / compliance
    authorized_signatory_name = models.CharField(max_length=255, blank=True)
    msme_registered = models.BooleanField(null=True, blank=True)
    msme_registration_number = models.CharField(max_length=100, blank=True)
    msme_enterprise_type = models.CharField(max_length=50, blank=True)
    declaration_accepted = models.BooleanField(null=True, blank=True)
    # JSON blocks
    contact_persons_json = models.JSONField(default=list, blank=True)
    head_office_address_json = models.JSONField(default=dict, blank=True)
    tax_registration_details_json = models.JSONField(default=dict, blank=True)
    # ── end approved profile fields ───────────────────────────────────────────

    sap_vendor_id = models.CharField(max_length=100)
    po_mandate_enabled = models.BooleanField(default=False)
    marketing_status = models.CharField(
        max_length=30,
        choices=MarketingStatus.choices,
        default=MarketingStatus.PENDING,
    )
    operational_status = models.CharField(
        max_length=30,
        choices=OperationalStatus.choices,
        default=OperationalStatus.INACTIVE,
    )
    approved_by_marketing = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="marketing_approved_vendors",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    # Portal access tracking
    portal_activation_sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the last activation email was sent",
    )
    portal_user_id = models.CharField(
        max_length=64, blank=True,
        help_text="User ID of the portal user (cached for display, not FK)",
    )
    portal_email = models.EmailField(
        blank=True,
        help_text="Cached portal user email for display",
    )
    # Profile revision hold state
    profile_change_pending = models.BooleanField(default=False)
    profile_hold_reason = models.CharField(max_length=500, blank=True)
    active_profile_revision = models.ForeignKey(
        "VendorProfileRevision",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="holding_vendor",
    )
    profile_hold_started_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vendors"
        ordering = ["vendor_name"]
        indexes = [
            models.Index(fields=["org", "operational_status"]),
            models.Index(fields=["scope_node", "operational_status"]),
            models.Index(fields=["sap_vendor_id"]),
        ]

    def __str__(self):
        return f"Vendor {self.id}: {self.vendor_name} [{self.operational_status}]"


# ---------------------------------------------------------------------------
# Vendor Profile Revision
# ---------------------------------------------------------------------------

class VendorProfileRevision(models.Model):
    """
    Tracks a proposed change to a Vendor's profile fields.

    While a revision is active (submitted through applied), the Vendor is placed
    on hold: new invoice submissions are blocked and in-flight workflow/finance
    approvals are frozen until the revision is applied or cancelled.
    """
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="profile_revisions",
    )
    revision_number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=30,
        choices=VendorProfileRevisionStatus.choices,
        default=VendorProfileRevisionStatus.DRAFT,
    )
    proposed_snapshot_json = models.JSONField(default=dict)
    changed_fields_json = models.JSONField(default=list)
    source_revision_snapshot_json = models.JSONField(default=dict)
    finance_sent_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_vendor_profile_revisions",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_vendor_profile_revisions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vendor_profile_revisions"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "revision_number"],
                name="unique_revision_number_per_vendor",
            ),
        ]
        indexes = [
            models.Index(fields=["vendor", "status"]),
        ]

    def __str__(self):
        return f"VendorProfileRevision {self.id}: vendor={self.vendor_id} rev={self.revision_number} [{self.status}]"


# ---------------------------------------------------------------------------
# Vendor User Binding
# ---------------------------------------------------------------------------

class UserVendorAssignment(models.Model):
    """
    Binds a portal user to a Vendor.  Exactly one active assignment per user/vendor pair.

    Created by approve_vendor_marketing() and re-used on re-activation.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vendor_assignments",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.CASCADE,
        related_name="user_assignments",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_vendor_assignments"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "vendor"],
                name="unique_user_vendor_assignment",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["vendor", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.vendor} [{'active' if self.is_active else 'inactive'}]"


# ---------------------------------------------------------------------------
# Vendor Portal Activation Token
# ---------------------------------------------------------------------------

class VendorActivationToken(models.Model):
    """
    Secure one-time activation token for vendor portal onboarding.

    Lifecycle:
      - uid  : used in the URL alongside token, maps to the vendor's email address
      - token: high-entropy secret (URL-safe)
      - expires_at: auto-expires after a configurable window (default 72 h)
      - used_at : set when password is set; subsequent use attempts fail
      - sent_at : when the activation email was sent
      - sent_by : which internal user triggered the resend (null for auto-trigger)
      - vendor : for display/cohort queries (denormalized)
    """
    uid = models.CharField(
        max_length=64,
        db_index=True,
        help_text="User ID (uid) used in the activation URL",
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the activation email was last sent",
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_vendor_activation_tokens",
    )
    vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.CASCADE,
        related_name="activation_tokens",
        null=True,
        blank=True,
        help_text="Denormalized vendor reference for display",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "vendor_activation_tokens"
        verbose_name = "Vendor Activation Token"
        verbose_name_plural = "Vendor Activation Tokens"
        indexes = [
            models.Index(fields=["uid", "token"]),
            models.Index(fields=["uid", "used_at"]),
        ]

    def __str__(self):
        return f"ActivationToken for uid={self.uid} (expires {self.expires_at.date()})"

    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def is_used(self) -> bool:
        return self.used_at is not None

    def is_valid(self) -> bool:
        return not self.is_expired() and not self.is_used()


# ---------------------------------------------------------------------------
# Vendor Submission Route (Send-To config)
# ---------------------------------------------------------------------------

class VendorSubmissionRoute(models.Model):
    """
    Maps a vendor-visible label (e.g. "Tarun") to a WorkflowTemplate.

    Scoped to org — all active routes in an org are available for any vendor
    invoice submission within that org.

    At submit time the template's currently published version is resolved.
    If no published version exists the submission is blocked, forcing proper
    enterprise configuration rather than silently falling back.
    """
    org = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="vendor_submission_routes",
    )
    code = models.CharField(
        max_length=100,
        help_text="Stable internal key, unique within org. Never shown to vendor.",
    )
    label = models.CharField(
        max_length=255,
        help_text="What the vendor sees, e.g. 'Tarun'.",
    )
    description = models.TextField(blank=True)
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    workflow_template = models.ForeignKey(
        "workflow.WorkflowTemplate",
        on_delete=models.PROTECT,
        related_name="vendor_submission_routes",
        help_text="Published version is resolved at submit time.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vendor_submission_routes"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "code"],
                name="unique_route_code_per_org",
            ),
        ]
        ordering = ["display_order", "label"]
        indexes = [
            models.Index(fields=["org", "is_active"]),
        ]

    def __str__(self):
        return f"Route {self.code} → '{self.label}' (template={self.workflow_template_id})"
