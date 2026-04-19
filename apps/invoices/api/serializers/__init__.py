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
    documents = serializers.SerializerMethodField()

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
            "final_invoice", "final_invoice_id",
            "documents",
            "created_at", "updated_at", "submitted_at",
        ]
        read_only_fields = [
            "id", "vendor", "submitted_by", "scope_node",
            "status", "source_file_name", "source_file_type",
            "confidence_score", "normalized_data", "validation_errors",
            "final_invoice", "documents",
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
    pass  # No body — finalises submission


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
