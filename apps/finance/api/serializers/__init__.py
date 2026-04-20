from rest_framework import serializers
from apps.finance.models import FinanceDecision, FinanceHandoff


class FinanceHandoffSerializer(serializers.ModelSerializer):
    subject_name = serializers.SerializerMethodField()
    recipient_emails = serializers.SerializerMethodField()
    recipient_count = serializers.SerializerMethodField()

    class Meta:
        model = FinanceHandoff
        fields = [
            "id", "org", "scope_node", "module", "subject_type", "subject_id",
            "subject_name",
            "status", "export_file", "submitted_by",
            "finance_reference_id", "sent_at", "created_at", "updated_at",
            "recipient_emails",
            "recipient_count",
        ]
        read_only_fields = fields

    def get_subject_name(self, obj) -> str:
        from apps.finance.services import _get_subject_name

        return _get_subject_name(obj)

    def get_recipient_emails(self, obj) -> list[str]:
        """
        Return the currently resolved recipient emails for this handoff.

        These are computed dynamically from the current role/scope state and
        are not a historical snapshot of who previously received the email.
        """
        from apps.finance.services import (
            NoFinanceRecipientsError,
            resolve_finance_recipients_for_handoff,
        )

        try:
            return resolve_finance_recipients_for_handoff(obj)
        except NoFinanceRecipientsError:
            return []

    def get_recipient_count(self, obj) -> int:
        return len(self.get_recipient_emails(obj))


class FinanceDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinanceDecision
        fields = [
            "id", "handoff", "decision", "reference_id", "note",
            "acted_via_token", "acted_at", "created_at",
        ]
        read_only_fields = fields


class FinanceApproveSerializer(serializers.Serializer):
    reference_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class FinanceRejectSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")


class PublicFinanceTokenSerializer(serializers.Serializer):
    """
    Public token metadata — returned by GET /api/v1/finance/public/{token}/
    Exposes only safe fields needed to render the approve/reject UI.
    """
    action_type = serializers.CharField()
    is_expired = serializers.BooleanField()
    is_used = serializers.BooleanField()
    module = serializers.CharField()
    subject_type = serializers.CharField()
    subject_name = serializers.CharField()
    handoff_status = serializers.CharField()


# ── Invoice Finance Review Serializers ─────────────────────────────────────────

class InvoiceFinanceDocumentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    file_name = serializers.CharField()
    document_type = serializers.CharField()
    uploaded_at = serializers.DateTimeField(allow_null=True)
    url = serializers.CharField(allow_null=True)  # None if not publicly accessible


class InvoiceFinanceVendorSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    vendor_name = serializers.CharField()
    email = serializers.CharField(allow_null=True)
    phone = serializers.CharField(allow_null=True)
    gstin = serializers.CharField(allow_null=True)
    pan = serializers.CharField(allow_null=True)
    sap_vendor_id = serializers.CharField(allow_null=True)


class InvoiceFinanceAllocationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    entity_name = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    category_name = serializers.CharField(allow_null=True)
    subcategory_name = serializers.CharField(allow_null=True)
    campaign_name = serializers.CharField(allow_null=True)
    budget_name = serializers.CharField(allow_null=True)
    selected_approver_email = serializers.CharField(allow_null=True)
    status = serializers.CharField()
    note = serializers.CharField(allow_null=True)


class InvoiceFinanceWorkflowStepSerializer(serializers.Serializer):
    name = serializers.CharField()
    status = serializers.CharField()
    assigned_user_email = serializers.CharField(allow_null=True)
    acted_at = serializers.DateTimeField(allow_null=True)
    note = serializers.CharField(allow_null=True)


class InvoiceFinanceWorkflowBranchSerializer(serializers.Serializer):
    entity_name = serializers.CharField()
    status = serializers.CharField()
    assigned_user_email = serializers.CharField(allow_null=True)
    acted_at = serializers.DateTimeField(allow_null=True)
    note = serializers.CharField(allow_null=True)


class InvoiceFinanceWorkflowGroupSerializer(serializers.Serializer):
    name = serializers.CharField()
    status = serializers.CharField()
    display_order = serializers.IntegerField()
    steps = InvoiceFinanceWorkflowStepSerializer(many=True)
    branches = InvoiceFinanceWorkflowBranchSerializer(many=True)


class InvoiceFinanceWorkflowSerializer(serializers.Serializer):
    instance_id = serializers.IntegerField(allow_null=True)
    status = serializers.CharField(allow_null=True)
    template_name = serializers.CharField(allow_null=True)
    version_number = serializers.IntegerField(allow_null=True)
    groups = InvoiceFinanceWorkflowGroupSerializer(many=True)


class InvoiceFinanceTimelineEventSerializer(serializers.Serializer):
    event_type = serializers.CharField()
    actor_email = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()
    metadata = serializers.JSONField()


class InvoiceFinanceHandoffDataSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()
    sent_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
    finance_reference_id = serializers.CharField(allow_null=True)
    recipient_count = serializers.IntegerField()
    recipient_emails = serializers.ListField(child=serializers.CharField())


class InvoiceFinanceInvoiceDataSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    currency = serializers.CharField()
    status = serializers.CharField()
    po_number = serializers.CharField(allow_null=True)
    vendor_invoice_number = serializers.CharField(allow_null=True)
    invoice_date = serializers.DateField(allow_null=True)
    due_date = serializers.DateField(allow_null=True)
    description = serializers.CharField(allow_null=True)
    scope_node_id = serializers.IntegerField()
    scope_node_name = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class InvoiceFinanceReviewSerializer(serializers.Serializer):
    """
    Rich invoice finance review payload for GET /api/v1/finance/public/{token}/
    when handoff.module == "invoice".
    """
    action_type = serializers.CharField()
    is_expired = serializers.BooleanField()
    is_used = serializers.BooleanField()
    module = serializers.CharField()
    subject_type = serializers.CharField()
    subject_name = serializers.CharField()
    handoff_status = serializers.CharField()
    handoff = InvoiceFinanceHandoffDataSerializer()
    invoice = InvoiceFinanceInvoiceDataSerializer()
    vendor = InvoiceFinanceVendorSerializer(allow_null=True)
    documents = InvoiceFinanceDocumentSerializer(many=True)
    allocations = InvoiceFinanceAllocationSerializer(many=True)
    workflow = InvoiceFinanceWorkflowSerializer(allow_null=True)
    timeline = InvoiceFinanceTimelineEventSerializer(many=True)
