from django.contrib import admin
from .models import Invoice


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "amount", "currency", "status", "scope_node", "created_by", "created_at")
    list_filter = ("status", "currency")
    search_fields = ("title",)
    raw_id_fields = ("scope_node", "created_by")
