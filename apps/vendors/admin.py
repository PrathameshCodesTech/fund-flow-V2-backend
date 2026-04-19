from django.contrib import admin

from .models import (
    Vendor, VendorInvitation, VendorOnboardingSubmission,
    VendorAttachment, VendorFinanceActionToken, VendorFinanceDecision,
)


@admin.register(VendorInvitation)
class VendorInvitationAdmin(admin.ModelAdmin):
    list_display = ("id", "vendor_email", "vendor_name_hint", "status", "org", "scope_node", "invited_by", "created_at")
    list_filter = ("status",)
    search_fields = ("vendor_email", "vendor_name_hint")
    raw_id_fields = ("org", "scope_node", "invited_by")
    readonly_fields = ("token", "created_at", "updated_at")


@admin.register(VendorOnboardingSubmission)
class VendorOnboardingSubmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "normalized_vendor_name", "normalized_email", "status", "submission_mode", "created_at")
    list_filter = ("status", "submission_mode")
    search_fields = ("normalized_vendor_name", "normalized_email")
    raw_id_fields = ("invitation",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(VendorAttachment)
class VendorAttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "document_type", "file_name", "submission", "uploaded_by", "created_at")
    raw_id_fields = ("submission", "uploaded_by")


@admin.register(VendorFinanceActionToken)
class VendorFinanceActionTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "action_type", "expires_at", "used_at", "created_at")
    list_filter = ("action_type",)
    raw_id_fields = ("submission",)
    readonly_fields = ("token", "created_at")


@admin.register(VendorFinanceDecision)
class VendorFinanceDecisionAdmin(admin.ModelAdmin):
    list_display = ("id", "submission", "decision", "sap_vendor_id", "acted_at", "created_at")
    list_filter = ("decision",)
    raw_id_fields = ("submission", "acted_via_token")


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("id", "vendor_name", "email", "sap_vendor_id", "operational_status", "marketing_status", "po_mandate_enabled", "org", "created_at")
    list_filter = ("operational_status", "marketing_status", "po_mandate_enabled")
    search_fields = ("vendor_name", "email", "sap_vendor_id")
    raw_id_fields = ("org", "scope_node", "onboarding_submission", "approved_by_marketing")
    readonly_fields = ("created_at", "updated_at")
