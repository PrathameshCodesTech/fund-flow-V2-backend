from rest_framework import serializers
from apps.invoices.models import (
    Invoice, InvoiceDocument, InvoiceDocumentType,
    InvoiceStatus, VendorInvoiceSubmission,
    VendorInvoiceSubmissionStatus,
)
from apps.core.models import ScopeNode
from apps.vendors.models import Vendor


# ---------------------------------------------------------------------------
# Invoice (existing — updated)
# ---------------------------------------------------------------------------

class InvoiceSerializer(serializers.ModelSerializer):
    selected_workflow_template_name = serializers.CharField(
        source="selected_workflow_template.name", read_only=True, default=None
    )
    selected_workflow_version_number = serializers.IntegerField(
        source="selected_workflow_version.version_number", read_only=True, default=None
    )
    workflow_selected_by_name = serializers.CharField(
        source="workflow_selected_by.name", read_only=True, default=None
    )
    workflow_instance_id = serializers.SerializerMethodField()
    workflow_instance_status = serializers.SerializerMethodField()
    can_record_payment = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id", "scope_node", "title", "amount", "currency",
            "status", "po_number", "vendor",
            "vendor_invoice_number", "invoice_date", "due_date",
            "subtotal_amount", "tax_amount", "description",
            "selected_workflow_template", "selected_workflow_version",
            "selected_workflow_template_name", "selected_workflow_version_number",
            "workflow_selected_by", "workflow_selected_by_name",
            "workflow_selected_at",
            "workflow_instance_id", "workflow_instance_status",
            "can_record_payment",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "status", "selected_workflow_template", "selected_workflow_version",
            "workflow_selected_by", "workflow_selected_at",
            "created_by", "created_at", "updated_at",
        ]

    def _get_workflow_instance(self, obj):
        from apps.workflow.models import WorkflowInstance, InstanceStatus

        return (
            WorkflowInstance.objects
            .filter(subject_type="invoice", subject_id=obj.pk)
            .exclude(status=InstanceStatus.REJECTED)
            .order_by("-id")
            .first()
        )

    def get_workflow_instance_id(self, obj):
        instance = self._get_workflow_instance(obj)
        return instance.id if instance else None

    def get_workflow_instance_status(self, obj):
        instance = self._get_workflow_instance(obj)
        return instance.status if instance else None

    def get_can_record_payment(self, obj):
        from apps.invoices.selectors import user_can_record_invoice_payment
        request = self.context.get("request")
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return user_can_record_invoice_payment(request.user, obj)


class InvoiceCreateSerializer(serializers.Serializer):
    """Serializer for invoice creation with PO mandate enforcement."""
    scope_node = serializers.PrimaryKeyRelatedField(queryset=ScopeNode.objects.all())
    title = serializers.CharField(max_length=255)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    currency = serializers.CharField(max_length=10, default="INR")
    po_number = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    vendor = serializers.PrimaryKeyRelatedField(
        queryset=Vendor.objects.all(),
        required=False,
        allow_null=True,
        default=None,
    )

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value


# ---------------------------------------------------------------------------
# VendorInvoiceSubmission
# ---------------------------------------------------------------------------

class VendorInvoiceSubmissionSerializer(serializers.ModelSerializer):
    confidence_percent = serializers.SerializerMethodField()
    final_invoice_id = serializers.CharField(read_only=True)
    vendor_name = serializers.CharField(source="vendor.vendor_name", read_only=True)
    scope_node_name = serializers.CharField(source="scope_node.name", read_only=True)
    submitted_by_name = serializers.CharField(source="submitted_by.name", read_only=True, default="")
    correction_requested_by_name = serializers.CharField(
        source="correction_requested_by.name", read_only=True, default=""
    )
    documents = serializers.SerializerMethodField()
    send_to_route_id = serializers.IntegerField(source="send_to_route.id", read_only=True, default=None)
    send_to_route_label = serializers.CharField(source="send_to_route.label", read_only=True, default=None)

    class Meta:
        model = VendorInvoiceSubmission
        fields = [
            "id", "vendor", "vendor_name",
            "submitted_by", "submitted_by_name",
            "scope_node", "scope_node_name",
            "status",
            "source_file_name", "source_file_type",
            "confidence_score", "confidence_percent",
            "normalized_data", "validation_errors",
            "correction_note", "correction_requested_by", "correction_requested_by_name",
            "correction_requested_at",
            "final_invoice", "final_invoice_id",
            "send_to_route_id", "send_to_route_label",
            "documents",
            "created_at", "updated_at", "submitted_at",
        ]
        read_only_fields = [
            "id", "vendor", "submitted_by", "scope_node",
            "status", "source_file_name", "source_file_type",
            "confidence_score", "normalized_data", "validation_errors",
            "correction_note", "correction_requested_by", "correction_requested_at",
            "final_invoice", "documents",
            "send_to_route_id", "send_to_route_label",
            "created_at", "updated_at", "submitted_at",
        ]

    def get_confidence_percent(self, obj):
        if obj.confidence_score is None:
            return None
        return round(float(obj.confidence_score) * 100, 1)

    def get_documents(self, obj):
        docs = obj.documents.all()
        return [
            {
                "id": d.id,
                "file_name": d.file_name,
                "file_type": d.file_type,
                "document_type": d.document_type,
                "created_at": d.created_at,
            }
            for d in docs
        ]


class VendorInvoiceSubmissionCreateSerializer(serializers.Serializer):
    """POST /api/v1/vendor-invoice-submissions/ — create with file upload."""
    scope_node = serializers.PrimaryKeyRelatedField(queryset=ScopeNode.objects.all())
    source_file = serializers.FileField()
    normalized_data = serializers.JSONField(required=False, default=None)


class VendorInvoiceSubmissionExtractSerializer(serializers.Serializer):
    pass  # No body — extraction re-runs on existing source file


class VendorInvoiceSubmissionUpdateSerializer(serializers.Serializer):
    """PATCH /api/v1/vendor-invoice-submissions/{id}/ — vendor corrects normalized fields."""
    normalized_data = serializers.JSONField()


class VendorInvoiceSubmissionSubmitSerializer(serializers.Serializer):
    """POST /api/v1/vendor-invoice-submissions/{id}/submit/ — new routed path."""
    send_to_option_id = serializers.IntegerField(
        help_text="ID of the active VendorSubmissionRoute chosen by the vendor.",
    )


# ---------------------------------------------------------------------------
# InvoiceDocument
# ---------------------------------------------------------------------------

class InvoiceDocumentSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceDocument
        fields = [
            "id", "invoice", "submission",
            "file_name", "file_type", "document_type",
            "download_url", "uploaded_by", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_download_url(self, obj):
        if obj.file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.file.url)
            return obj.file.url
        return ""


class InvoiceDocumentCreateSerializer(serializers.Serializer):
    """POST /api/v1/vendor-invoice-submissions/{id}/documents/"""
    file = serializers.FileField()
    document_type = serializers.ChoiceField(choices=InvoiceDocumentType.choices)


# ---------------------------------------------------------------------------
# InvoicePayment
# ---------------------------------------------------------------------------

class InvoicePaymentSerializer(serializers.ModelSerializer):
    """Full payment record — internal use."""
    recorded_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    can_record_payment = serializers.SerializerMethodField()
    # Internal-only bank fields NOT exposed to vendor
    payer_bank_name = serializers.CharField(read_only=True)
    beneficiary_name = serializers.CharField(read_only=True)
    beneficiary_bank_name = serializers.CharField(read_only=True)

    class Meta:
        model = None  # Set in __init__
        fields = [
            "id", "invoice",
            "payment_status", "payment_method",
            "payment_reference_number", "utr_number",
            "transaction_id", "bank_reference_number",
            "payer_bank_name", "beneficiary_name", "beneficiary_bank_name",
            "paid_amount", "currency", "payment_date",
            "remarks",
            "recorded_by", "recorded_by_name", "recorded_at",
            "updated_by", "updated_by_name", "updated_at",
            "can_record_payment",
        ]
        read_only_fields = fields

    def __init__(self, *args, **kwargs):
        from apps.invoices.models import InvoicePayment
        super().__init__(*args, **kwargs)
        self.Meta.model = InvoicePayment

    def get_recorded_by_name(self, obj):
        if obj.recorded_by:
            full = obj.recorded_by.get_full_name()
            return full if full else obj.recorded_by.username
        return ""

    def get_updated_by_name(self, obj):
        if obj.updated_by:
            full = obj.updated_by.get_full_name()
            return full if full else obj.updated_by.username
        return ""

    def get_can_record_payment(self, obj):
        from apps.invoices.selectors import user_can_record_invoice_payment
        request = self.context.get("request")
        if not request or not request.user:
            return False
        return user_can_record_invoice_payment(request.user, obj.invoice)


class VendorInvoicePaymentSerializer(serializers.ModelSerializer):
    """Vendor-safe payment record — excludes internal bank/audit fields."""

    class Meta:
        model = None  # Set in __init__
        fields = [
            "payment_status",
            "payment_method",
            "payment_reference_number",
            "utr_number",
            "paid_amount",
            "currency",
            "payment_date",
            "remarks",
        ]
        read_only_fields = fields

    def __init__(self, *args, **kwargs):
        from apps.invoices.models import InvoicePayment
        super().__init__(*args, **kwargs)
        self.Meta.model = InvoicePayment


class InvoicePaymentUpdateSerializer(serializers.Serializer):
    """PUT/PATCH for recording or updating a payment."""
    payment_status = serializers.ChoiceField(
        choices=["pending", "paid", "failed", "reversed"],
        required=False,
    )
    payment_method = serializers.ChoiceField(
        choices=["bank_transfer", "rtgs", "neft", "imps", "upi", "cheque", "other"],
        required=False,
        allow_blank=True,
    )
    payment_reference_number = serializers.CharField(max_length=255, required=False, allow_blank=True)
    utr_number = serializers.CharField(max_length=255, required=False, allow_blank=True)
    transaction_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    bank_reference_number = serializers.CharField(max_length=255, required=False, allow_blank=True)
    payer_bank_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    beneficiary_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    beneficiary_bank_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    paid_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    currency = serializers.CharField(max_length=10, required=False)
    payment_date = serializers.DateField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        payment_status = attrs.get("payment_status")
        if payment_status == "paid":
            from apps.invoices.models import PaymentMethod
            errors = {}
            if not attrs.get("payment_method"):
                errors.setdefault("payment_method", []).append("Payment method is required when marking as paid.")
            if not attrs.get("payment_date"):
                errors.setdefault("payment_date", []).append("Payment date is required when marking as paid.")
            amount = attrs.get("paid_amount")
            if not amount or amount <= 0:
                errors.setdefault("paid_amount", []).append("Paid amount must be greater than zero when marking as paid.")
            ref = attrs.get("payment_reference_number", "").strip()
            utr = attrs.get("utr_number", "").strip()
            if not ref and not utr:
                errors.setdefault("utr_number", []).append(
                    "At least one of payment_reference_number or utr_number is required when marking as paid."
                )
            if errors:
                raise serializers.ValidationError(errors)
        return attrs
