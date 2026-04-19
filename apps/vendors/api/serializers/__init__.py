from rest_framework import serializers

from apps.vendors.models import (
    FinanceActionType,
    Vendor,
    VendorAttachment,
    VendorFinanceActionToken,
    VendorFinanceDecision,
    VendorInvitation,
    VendorOnboardingSubmission,
)


# ---------------------------------------------------------------------------
# VendorInvitation
# ---------------------------------------------------------------------------

class VendorInvitationSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorInvitation
        fields = [
            "id", "org", "scope_node", "invited_by",
            "vendor_email", "vendor_name_hint",
            "status", "expires_at", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]


class VendorInvitationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorInvitation
        fields = ["org", "scope_node", "vendor_email", "vendor_name_hint", "expires_at"]


# ---------------------------------------------------------------------------
# VendorOnboardingSubmission
# ---------------------------------------------------------------------------

class VendorSubmissionSerializer(serializers.ModelSerializer):
    has_source_excel = serializers.SerializerMethodField()
    has_exported_excel = serializers.SerializerMethodField()

    class Meta:
        model = VendorOnboardingSubmission
        fields = [
            "id", "invitation", "submission_mode", "status",
            "raw_form_data",
            "normalized_vendor_name", "normalized_vendor_type",
            "normalized_email", "normalized_phone",
            "normalized_gst_registered", "normalized_gstin", "normalized_pan",
            "normalized_address_line1", "normalized_address_line2",
            "normalized_city", "normalized_state", "normalized_country", "normalized_pincode",
            "normalized_bank_name", "normalized_account_number", "normalized_ifsc",
            "has_source_excel", "has_exported_excel",
            "finance_sent_at", "finance_vendor_code",
            "submitted_at", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_has_source_excel(self, obj):
        import os
        return bool(obj.source_excel_file and os.path.isfile(obj.source_excel_file))

    def get_has_exported_excel(self, obj):
        import os
        return bool(obj.exported_excel_file and os.path.isfile(obj.exported_excel_file))


# ---------------------------------------------------------------------------
# VendorAttachment
# ---------------------------------------------------------------------------

class VendorAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = VendorAttachment
        fields = [
            "id", "submission", "document_type", "title",
            "file_name", "file_url", "uploaded_by", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_file_url(self, obj):
        if obj.file:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.file.url)
            return obj.file.url
        return obj.file_url or ""


class VendorAttachmentCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    document_type = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")


# ---------------------------------------------------------------------------
# VendorFinanceActionToken (read-only metadata)
# ---------------------------------------------------------------------------

class VendorFinanceActionTokenSerializer(serializers.ModelSerializer):
    is_expired = serializers.SerializerMethodField()
    is_used = serializers.SerializerMethodField()

    class Meta:
        model = VendorFinanceActionToken
        fields = [
            "id", "submission", "action_type",
            "expires_at", "used_at", "created_at",
            "is_expired", "is_used",
        ]
        read_only_fields = fields

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_is_used(self, obj):
        return obj.is_used()


# ---------------------------------------------------------------------------
# VendorFinanceDecision
# ---------------------------------------------------------------------------

class VendorFinanceDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorFinanceDecision
        fields = [
            "id", "submission", "decision",
            "sap_vendor_id", "note",
            "acted_via_token", "acted_at", "created_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Vendor master
# ---------------------------------------------------------------------------

class VendorSerializer(serializers.ModelSerializer):
    scope_node_name = serializers.CharField(source="scope_node.name", read_only=True)
    org_name = serializers.CharField(source="org.name", read_only=True, default="")
    portal_activated = serializers.SerializerMethodField()

    class Meta:
        model = Vendor
        fields = [
            "id", "org", "org_name", "scope_node", "scope_node_name",
            "onboarding_submission",
            "vendor_name", "email", "phone", "sap_vendor_id",
            "po_mandate_enabled",
            "marketing_status", "operational_status",
            "approved_by_marketing", "approved_at",
            "portal_email", "portal_activation_sent_at", "portal_user_id",
            "portal_activated",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "org", "scope_node", "onboarding_submission",
            "sap_vendor_id", "marketing_status", "operational_status",
            "approved_by_marketing", "approved_at",
            "portal_email", "portal_activation_sent_at", "portal_user_id",
            "portal_activated",
            "created_at", "updated_at",
        ]

    def get_portal_activated(self, obj):
        """True if vendor has an active portal user with a used activation token."""
        from apps.vendors.models import VendorActivationToken
        if not obj.portal_user_id:
            return False
        return VendorActivationToken.objects.filter(
            uid=obj.portal_user_id, used_at__isnull=False
        ).exists()


class VendorUpdateSerializer(serializers.ModelSerializer):
    """Allows patching only safe fields."""
    class Meta:
        model = Vendor
        fields = ["po_mandate_enabled", "email", "phone"]


# ---------------------------------------------------------------------------
# Action serializers
# ---------------------------------------------------------------------------

class SendToFinanceSerializer(serializers.Serializer):
    pass  # No extra fields; action is purely state transition


class ReopenSubmissionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")


class FinanceApproveSerializer(serializers.Serializer):
    sap_vendor_id = serializers.CharField(max_length=100)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class FinanceRejectSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")


class MarketingApproveSerializer(serializers.Serializer):
    po_mandate_enabled = serializers.BooleanField(default=False)


class MarketingRejectSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")


class ManualSubmissionSerializer(serializers.Serializer):
    """
    Flexible payload for manual vendor form submission.
    Accepts any key-value pairs via the 'data' dict, plus an optional finalize flag.
    """
    data = serializers.DictField(child=serializers.JSONField(), default=dict)
    finalize = serializers.BooleanField(default=False)


class FinalizeSerializer(serializers.Serializer):
    pass  # Finalize is a standalone action with no extra body


# ---------------------------------------------------------------------------
# Public: invitation metadata (safe fields only — no token exposed)
# ---------------------------------------------------------------------------

class PublicInvitationSerializer(serializers.ModelSerializer):
    scope_node_name = serializers.CharField(source="scope_node.name", read_only=True)

    class Meta:
        model = VendorInvitation
        fields = [
            "id", "vendor_email", "vendor_name_hint",
            "scope_node_name", "status",
        ]


# ---------------------------------------------------------------------------
# Public: finance action token metadata (full review payload)
# ---------------------------------------------------------------------------

class PublicFinanceTokenSerializer(serializers.ModelSerializer):
    # Token state
    is_expired = serializers.SerializerMethodField()
    is_used = serializers.SerializerMethodField()

    # Token state
    is_expired = serializers.SerializerMethodField()
    is_used = serializers.SerializerMethodField()

    # Submission summary fields
    submission_id = serializers.IntegerField(source="submission.id", read_only=True)
    submission_status = serializers.CharField(source="submission.status", read_only=True)
    vendor_name = serializers.CharField(source="submission.normalized_vendor_name", read_only=True)
    vendor_email = serializers.CharField(source="submission.normalized_email", read_only=True)
    vendor_phone = serializers.CharField(source="submission.normalized_phone", read_only=True)
    vendor_type = serializers.CharField(source="submission.normalized_vendor_type", read_only=True)
    gstin = serializers.CharField(source="submission.normalized_gstin", read_only=True)
    pan = serializers.CharField(source="submission.normalized_pan", read_only=True)
    address_line1 = serializers.CharField(source="submission.normalized_address_line1", read_only=True)
    address_line2 = serializers.CharField(source="submission.normalized_address_line2", read_only=True)
    city = serializers.CharField(source="submission.normalized_city", read_only=True)
    state = serializers.CharField(source="submission.normalized_state", read_only=True)
    country = serializers.CharField(source="submission.normalized_country", read_only=True)
    pincode = serializers.CharField(source="submission.normalized_pincode", read_only=True)
    bank_name = serializers.CharField(source="submission.normalized_bank_name", read_only=True)
    account_number = serializers.CharField(source="submission.normalized_account_number", read_only=True)
    ifsc = serializers.CharField(source="submission.normalized_ifsc", read_only=True)

    # Safe file availability flags + download URLs (no raw filesystem paths exposed)
    has_exported_excel = serializers.SerializerMethodField()
    exported_excel_download_url = serializers.SerializerMethodField()
    has_source_excel = serializers.SerializerMethodField()
    source_excel_download_url = serializers.SerializerMethodField()

    # Supporting attachments (with per-attachment download URLs)
    attachments = serializers.SerializerMethodField()

    # Paired reject token (only populated when action_type=approve)
    reject_token = serializers.SerializerMethodField()

    class Meta:
        model = VendorFinanceActionToken
        fields = [
            "action_type", "expires_at", "is_expired", "is_used",
            "submission_id", "submission_status",
            "vendor_name", "vendor_email", "vendor_phone", "vendor_type",
            "gstin", "pan",
            "address_line1", "address_line2", "city", "state", "country", "pincode",
            "bank_name", "account_number", "ifsc",
            "has_exported_excel", "exported_excel_download_url",
            "has_source_excel", "source_excel_download_url",
            "attachments", "reject_token",
        ]

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_is_used(self, obj):
        return obj.is_used()

    def get_has_exported_excel(self, obj):
        import os
        path = obj.submission.exported_excel_file
        return bool(path and os.path.isfile(path))

    def get_exported_excel_download_url(self, obj):
        import os
        path = obj.submission.exported_excel_file
        if path and os.path.isfile(path):
            return f"/api/v1/vendors/public/finance/{obj.token}/download/export-excel/"
        return None

    def get_has_source_excel(self, obj):
        import os
        path = obj.submission.source_excel_file
        return bool(path and os.path.isfile(path))

    def get_source_excel_download_url(self, obj):
        import os
        path = obj.submission.source_excel_file
        if path and os.path.isfile(path):
            return f"/api/v1/vendors/public/finance/{obj.token}/download/source-excel/"
        return None

    def get_attachments(self, obj):
        result = []
        for att in obj.submission.attachments.all():
            has_file = bool(att.file and att.file.name)
            result.append({
                "id": att.id,
                "title": att.title,
                "file_name": att.file_name,
                "document_type": att.document_type,
                "download_url": (
                    f"/api/v1/vendors/public/finance/{obj.token}/download/attachment/{att.id}/"
                    if has_file else None
                ),
            })
        return result

    def get_reject_token(self, obj):
        if obj.action_type == FinanceActionType.APPROVE:
            return (
                obj.submission.finance_tokens
                .filter(action_type=FinanceActionType.REJECT)
                .values_list("token", flat=True)
                .first()
            )
        return None
