from django.conf import settings
from django.db import models


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_WORKFLOW = "pending_workflow", "Pending Workflow"
    PENDING = "pending", "Pending"
    IN_REVIEW = "in_review", "In Review"
    INTERNALLY_APPROVED = "internally_approved", "Internally Approved"
    FINANCE_PENDING = "finance_pending", "Finance Pending"
    FINANCE_APPROVED = "finance_approved", "Finance Approved"
    FINANCE_REJECTED = "finance_rejected", "Finance Rejected"
    REJECTED = "rejected", "Rejected"
    PAID = "paid", "Paid"


class Invoice(models.Model):
    """
    Module subject. scope_node is the anchor for all workflow context derivation.
    paid is a business state on this model, not a workflow step.
    """
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="invoices",
        help_text="Entity or company this invoice belongs to",
    )
    title = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    status = models.CharField(
        max_length=20,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.DRAFT,
    )
    po_number = models.CharField(max_length=100, blank=True, help_text="Purchase Order number — required if vendor has PO mandate")
    vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices",
        help_text="Bound vendor (populated for portal-created invoices)",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_invoices",
    )
    # Vendor-supplied invoice metadata
    vendor_invoice_number = models.CharField(
        max_length=255, blank=True,
        help_text="Vendor's own invoice reference number",
    )
    invoice_date = models.DateField(
        null=True, blank=True,
        help_text="Date on the vendor's invoice",
    )
    due_date = models.DateField(null=True, blank=True, help_text="Payment due date")
    subtotal_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Pre-tax subtotal",
    )
    tax_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Tax amount",
    )
    description = models.TextField(blank=True, help_text="Invoice description / notes")
    # Explicit workflow attachment (set by internal user before runtime starts)
    selected_workflow_template = models.ForeignKey(
        "workflow.WorkflowTemplate",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="selected_invoices",
    )
    selected_workflow_version = models.ForeignKey(
        "workflow.WorkflowTemplateVersion",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="selected_invoices",
    )
    workflow_selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="workflow_selections",
    )
    workflow_selected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoices"
        indexes = [
            models.Index(fields=["scope_node", "status"]),
            models.Index(fields=["vendor"]),
        ]

    def __str__(self):
        return f"Invoice {self.id}: {self.title} [{self.status}]"


# ---------------------------------------------------------------------------
# Vendor Invoice Submission intake layer
# ---------------------------------------------------------------------------

class VendorInvoiceSubmissionStatus(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    EXTRACTING = "extracting", "Extracting"
    NEEDS_CORRECTION = "needs_correction", "Needs Correction"
    READY = "ready", "Ready"
    SUBMITTED = "submitted", "Submitted"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"


class VendorInvoiceSubmission(models.Model):
    """
    Intake layer for vendor-submitted invoices.

    Lifecycle:
      1. Vendor uploads PDF/XLSX → status=uploaded, source_file stored
      2. Backend extracts data   → status=extracting then needs_correction/ready
      3. Vendor corrects fields   → PATCH to update normalized_data
      4. Vendor submits          → final Invoice created, status=submitted

    Vendor is the business Vendor record, not the portal User.
    submitted_by is the portal user who performed the upload action.
    """
    vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.PROTECT,
        related_name="invoice_submissions",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="vendor_invoice_submissions",
    )
    scope_node = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="vendor_invoice_submissions",
    )
    status = models.CharField(
        max_length=30,
        choices=VendorInvoiceSubmissionStatus.choices,
        default=VendorInvoiceSubmissionStatus.UPLOADED,
        db_index=True,
    )
    # Source file
    source_file = models.FileField(
        upload_to="vendor_invoice_submissions/source_files/",
        blank=True, null=True,
    )
    source_file_name = models.CharField(max_length=500, blank=True)
    source_file_type = models.CharField(
        max_length=10,
        choices=[("pdf", "PDF"), ("xlsx", "Excel"), ("xls", "Excel")],
    )
    source_file_hash = models.CharField(max_length=64, blank=True)
    # Extraction
    raw_extracted_data = models.JSONField(default=dict, blank=True)
    normalized_data = models.JSONField(default=dict, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    confidence_score = models.DecimalField(
        max_digits=5, decimal_places=3, null=True, blank=True,
    )
    # Final invoice
    final_invoice = models.OneToOneField(
        "invoices.Invoice",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="submission",
    )
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "vendor_invoice_submissions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["vendor", "status"], name="vis_vendor_status_idx"),
            models.Index(fields=["vendor", "source_file_hash"], name="vis_vendor_hash_idx"),
            models.Index(fields=["submitted_by"], name="vis_submitted_by_idx"),
        ]

    def __str__(self):
        return f"VendorInvoiceSubmission {self.id} [{self.status}]"


# ---------------------------------------------------------------------------
# Invoice Allocation (runtime split)
# ---------------------------------------------------------------------------

class InvoiceAllocationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    BRANCH_PENDING = "branch_pending", "Branch Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    CORRECTION_REQUIRED = "correction_required", "Correction Required"
    CANCELLED = "cancelled", "Cancelled"


class InvoiceAllocation(models.Model):
    """
    First-class business object representing one line of a runtime invoice split.
    Created when the assigned splitter submits allocation lines at a
    RUNTIME_SPLIT_ALLOCATION workflow step. One allocation = one branch task.
    """
    invoice = models.ForeignKey(
        "invoices.Invoice",
        on_delete=models.CASCADE,
        related_name="allocations",
    )
    workflow_instance = models.ForeignKey(
        "workflow.WorkflowInstance",
        on_delete=models.CASCADE,
        related_name="invoice_allocations",
    )
    split_step = models.ForeignKey(
        "workflow.WorkflowInstanceStep",
        on_delete=models.CASCADE,
        related_name="invoice_allocations",
        help_text="The RUNTIME_SPLIT_ALLOCATION instance step that owns this allocation",
    )
    branch = models.OneToOneField(
        "workflow.WorkflowInstanceBranch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_allocation",
        help_text="Branch task created for this allocation",
    )
    entity = models.ForeignKey(
        "core.ScopeNode",
        on_delete=models.PROTECT,
        related_name="invoice_allocations",
        help_text="The scope node (entity) this allocation is assigned to",
    )
    category = models.ForeignKey(
        "budgets.BudgetCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_allocations",
    )
    subcategory = models.ForeignKey(
        "budgets.BudgetSubCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_allocations",
    )
    campaign = models.ForeignKey(
        "campaigns.Campaign",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_allocations",
    )
    budget = models.ForeignKey(
        "budgets.Budget",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_allocations",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    percentage = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    selected_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approver_allocations",
    )
    status = models.CharField(
        max_length=25,
        choices=InvoiceAllocationStatus.choices,
        default=InvoiceAllocationStatus.DRAFT,
    )
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="selected_allocations",
    )
    selected_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_allocations",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rejected_allocations",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    note = models.TextField(blank=True)
    revision_number = models.PositiveIntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoice_allocations"
        indexes = [
            models.Index(fields=["invoice", "status"]),
            models.Index(fields=["workflow_instance"]),
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["budget", "status"]),
        ]

    def __str__(self):
        return f"Allocation {self.id}: invoice={self.invoice_id} entity={self.entity_id} amount={self.amount} [{self.status}]"


class InvoiceAllocationRevision(models.Model):
    """Snapshot of an InvoiceAllocation at the time of each correction cycle."""
    allocation = models.ForeignKey(
        InvoiceAllocation,
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField()
    snapshot = models.JSONField(help_text="Full allocation field snapshot at this revision")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="allocation_revisions",
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    change_reason = models.TextField(blank=True)

    class Meta:
        db_table = "invoice_allocation_revisions"
        ordering = ["revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["allocation", "revision_number"],
                name="unique_revision_per_allocation",
            ),
        ]

    def __str__(self):
        return f"Revision {self.revision_number} for Allocation {self.allocation_id}"


class InvoiceDocumentType(models.TextChoices):
    INVOICE_PDF = "invoice_pdf", "Invoice PDF"
    INVOICE_EXCEL = "invoice_excel", "Invoice Excel"
    PO_COPY = "po_copy", "PO Copy"
    DELIVERY_CHALLAN = "delivery_challan", "Delivery Challan"
    TAX_DOCUMENT = "tax_document", "Tax Document"
    SUPPORTING_DOCUMENT = "supporting_document", "Supporting Document"


class InvoiceDocument(models.Model):
    """
    Supporting document attached to a vendor invoice submission.
    Once the final Invoice is created, invoice FK is populated.
    """
    invoice = models.ForeignKey(
        "invoices.Invoice",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="documents",
    )
    submission = models.ForeignKey(
        VendorInvoiceSubmission,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    file = models.FileField(
        upload_to="vendor_invoice_documents/files/",
        blank=True, null=True,
    )
    file_name = models.CharField(max_length=500, blank=True)
    file_type = models.CharField(
        max_length=10,
        choices=[
            ("pdf", "PDF"), ("xlsx", "Excel"), ("xls", "Excel"),
            ("png", "PNG"), ("jpg", "JPG"), ("jpeg", "JPEG"),
        ],
    )
    document_type = models.CharField(
        max_length=30,
        choices=InvoiceDocumentType.choices,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="vendor_invoice_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "invoice_documents"
        ordering = ["-created_at"]

    def __str__(self):
        return f"InvoiceDocument {self.id}: {self.file_name}"
