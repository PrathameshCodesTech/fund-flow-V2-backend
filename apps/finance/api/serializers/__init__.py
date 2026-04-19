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
