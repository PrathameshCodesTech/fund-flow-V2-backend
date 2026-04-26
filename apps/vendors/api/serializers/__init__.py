from rest_framework import serializers

from apps.vendors.models import (
    ALLOWED_ATTACHMENT_DOCUMENT_TYPES,
    FinanceActionType,
    Vendor,
    VendorAttachment,
    VendorFinanceActionToken,
    VendorFinanceDecision,
    VendorInvitation,
    VendorOnboardingSubmission,
    VendorProfileRevision,
    VendorSubmissionRoute,
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
            # Core identity
            "normalized_title",
            "normalized_vendor_name", "normalized_vendor_type",
            "normalized_email", "normalized_phone", "normalized_fax",
            "normalized_gst_registered", "normalized_gstin", "normalized_pan",
            "normalized_region", "normalized_head_office_no",
            # Address
            "normalized_address_line1", "normalized_address_line2", "normalized_address_line3",
            "normalized_city", "normalized_state", "normalized_country", "normalized_pincode",
            # Bank core
            "normalized_preferred_payment_mode",
            "normalized_beneficiary_name",
            "normalized_bank_name", "normalized_account_number", "normalized_bank_account_type",
            "normalized_ifsc", "normalized_micr_code", "normalized_neft_code",
            # Bank branch contact
            "normalized_bank_branch_address_line1", "normalized_bank_branch_address_line2",
            "normalized_bank_branch_city", "normalized_bank_branch_state",
            "normalized_bank_branch_country", "normalized_bank_branch_pincode",
            "normalized_bank_phone", "normalized_bank_fax",
            # MSME / compliance
            "normalized_authorized_signatory_name",
            "normalized_msme_registered", "normalized_msme_registration_number",
            "normalized_msme_enterprise_type", "declaration_accepted",
            # Structured JSON blocks
            "contact_persons_json", "head_office_address_json", "tax_registration_details_json",
            # File tracking
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

    def validate_document_type(self, value):
        if value and value not in ALLOWED_ATTACHMENT_DOCUMENT_TYPES:
            raise serializers.ValidationError(
                f"document_type '{value}' is not allowed. "
                f"Accepted types: {', '.join(sorted(ALLOWED_ATTACHMENT_DOCUMENT_TYPES))}"
            )
        return value


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
            # Core identity
            "vendor_name", "email", "phone",
            # Approved live profile (read-only — updated via profile revision)
            "title", "vendor_type", "fax", "region", "head_office_no",
            "gst_registered", "gstin", "pan",
            # Address
            "address_line1", "address_line2", "address_line3",
            "city", "state", "country", "pincode",
            # Bank
            "preferred_payment_mode", "beneficiary_name", "bank_name",
            "account_number", "bank_account_type", "ifsc", "micr_code", "neft_code",
            # Bank branch
            "bank_branch_address_line1", "bank_branch_address_line2",
            "bank_branch_city", "bank_branch_state",
            "bank_branch_country", "bank_branch_pincode",
            "bank_phone", "bank_fax",
            # MSME / compliance
            "authorized_signatory_name", "msme_registered",
            "msme_registration_number", "msme_enterprise_type",
            "declaration_accepted",
            # JSON blocks
            "contact_persons_json", "head_office_address_json",
            "tax_registration_details_json",
            # System fields
            "sap_vendor_id", "po_mandate_enabled",
            "marketing_status", "operational_status",
            "approved_by_marketing", "approved_at",
            "portal_email", "portal_activation_sent_at", "portal_user_id",
            "portal_activated",
            "profile_change_pending", "profile_hold_reason",
            "active_profile_revision", "profile_hold_started_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "org", "scope_node", "onboarding_submission",
            # All profile fields are read-only here
            "title", "vendor_type", "fax", "region", "head_office_no",
            "gst_registered", "gstin", "pan",
            "address_line1", "address_line2", "address_line3",
            "city", "state", "country", "pincode",
            "preferred_payment_mode", "beneficiary_name", "bank_name",
            "account_number", "bank_account_type", "ifsc", "micr_code", "neft_code",
            "bank_branch_address_line1", "bank_branch_address_line2",
            "bank_branch_city", "bank_branch_state",
            "bank_branch_country", "bank_branch_pincode",
            "bank_phone", "bank_fax",
            "authorized_signatory_name", "msme_registered",
            "msme_registration_number", "msme_enterprise_type",
            "declaration_accepted",
            "contact_persons_json", "head_office_address_json",
            "tax_registration_details_json",
            "sap_vendor_id", "marketing_status", "operational_status",
            "approved_by_marketing", "approved_at",
            "portal_email", "portal_activation_sent_at", "portal_user_id",
            "portal_activated",
            "profile_change_pending", "profile_hold_reason",
            "active_profile_revision", "profile_hold_started_at",
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

    def validate_data(self, value):
        from apps.vendors.models import ALLOWED_MSME_ENTERPRISE_TYPES
        et = (
            value.get("msme_enterprise_type")
            or value.get("msme_enterprise_type".replace("_", " "))
            or value.get("enterprise_type")
            or value.get("enterprise_type".replace("_", " "))
            or value.get("enterprise type")
            or value.get("enterprise  type")
            or ""
        )
        et = str(et).strip().lower()
        if et and et not in ALLOWED_MSME_ENTERPRISE_TYPES:
            raise serializers.ValidationError(
                f"msme_enterprise_type must be one of {sorted(ALLOWED_MSME_ENTERPRISE_TYPES)}; "
                f"got '{et}'."
            )
        return value


class FinalizeSerializer(serializers.Serializer):
    pass  # Finalize is a standalone action with no extra body


# ---------------------------------------------------------------------------
# VendorSubmissionRoute
# ---------------------------------------------------------------------------

class VendorSubmissionRouteSerializer(serializers.ModelSerializer):
    """Full representation for internal admin CRUD."""
    workflow_template_name = serializers.CharField(
        source="workflow_template.name", read_only=True
    )
    workflow_template_code = serializers.CharField(
        source="workflow_template.code", read_only=True
    )
    published_version_id = serializers.SerializerMethodField()
    published_version_number = serializers.SerializerMethodField()

    class Meta:
        model = VendorSubmissionRoute
        fields = [
            "id", "org", "code", "label", "description", "display_order",
            "is_active", "workflow_template",
            "workflow_template_name", "workflow_template_code",
            "published_version_id", "published_version_number",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_published_version_id(self, obj):
        from apps.workflow.models import WorkflowTemplateVersion, VersionStatus
        v = WorkflowTemplateVersion.objects.filter(
            template=obj.workflow_template, status=VersionStatus.PUBLISHED
        ).order_by("-version_number").first()
        return v.id if v else None

    def get_published_version_number(self, obj):
        from apps.workflow.models import WorkflowTemplateVersion, VersionStatus
        v = WorkflowTemplateVersion.objects.filter(
            template=obj.workflow_template, status=VersionStatus.PUBLISHED
        ).order_by("-version_number").first()
        return v.version_number if v else None


class VendorSubmissionRouteCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorSubmissionRoute
        fields = [
            "org", "code", "label", "description", "display_order",
            "is_active", "workflow_template",
        ]

    def validate(self, data):
        template = data.get("workflow_template")
        org = data.get("org")
        if template:
            if template.module != "invoice":
                raise serializers.ValidationError({
                    "workflow_template": (
                        f"The workflow template has module '{template.module}'. "
                        "Only 'invoice' templates may be mapped to a send-to route."
                    ),
                })
            if org and template.scope_node.org_id != org.id:
                raise serializers.ValidationError({
                    "workflow_template": (
                        "The workflow template belongs to a different org than the route. "
                        "Template and route must share the same org."
                    ),
                })
        return data


class VendorSubmissionRouteUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorSubmissionRoute
        fields = ["label", "description", "display_order", "is_active", "workflow_template"]

    def validate_workflow_template(self, value):
        if value.module != "invoice":
            raise serializers.ValidationError(
                f"The workflow template has module '{value.module}'. "
                "Only 'invoice' templates may be mapped to a send-to route."
            )
        return value

    def validate(self, data):
        template = data.get("workflow_template")
        if template and self.instance:
            if template.scope_node.org_id != self.instance.org_id:
                raise serializers.ValidationError({
                    "workflow_template": (
                        "The workflow template belongs to a different org than the route. "
                        "Template and route must share the same org."
                    ),
                })
        return data


class VendorSubmissionRouteVendorSerializer(serializers.ModelSerializer):
    """Minimal representation shown to vendors — no template internals exposed."""
    class Meta:
        model = VendorSubmissionRoute
        fields = ["id", "code", "label", "display_order"]


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


# ---------------------------------------------------------------------------
# VendorProfileRevision
# ---------------------------------------------------------------------------

class VendorProfileRevisionSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = VendorProfileRevision
        fields = [
            "id", "vendor", "revision_number", "status",
            "proposed_snapshot_json", "changed_fields_json", "source_revision_snapshot_json",
            "finance_sent_at", "submitted_at", "approved_at", "applied_at",
            "created_by", "created_by_name", "updated_by", "updated_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None

    def get_updated_by_name(self, obj):
        if obj.updated_by:
            return obj.updated_by.get_full_name() or obj.updated_by.email
        return None


class VendorProfileRevisionListSerializer(serializers.ModelSerializer):
    """Compact representation for list views."""
    class Meta:
        model = VendorProfileRevision
        fields = [
            "id", "vendor", "revision_number", "status",
            "changed_fields_json", "submitted_at", "applied_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields


class SaveDraftRevisionSerializer(serializers.Serializer):
    proposed_snapshot = serializers.DictField(required=True)


class RejectRevisionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
