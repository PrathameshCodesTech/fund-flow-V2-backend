from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "action", "resource_type", "resource_id", "created_at")
    list_filter = ("action", "resource_type")
    search_fields = ("resource_type", "action")
    raw_id_fields = ("user",)
