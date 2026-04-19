from django.contrib import admin
from .models import Campaign, CampaignDocument


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "status", "org", "scope_node", "requested_amount", "created_by", "created_at")
    list_filter = ("status", "org")
    search_fields = ("name", "code")
    raw_id_fields = ("org", "scope_node", "category", "subcategory", "budget", "created_by")


@admin.register(CampaignDocument)
class CampaignDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "campaign", "document_type", "uploaded_by", "created_at")
    raw_id_fields = ("campaign", "uploaded_by")
