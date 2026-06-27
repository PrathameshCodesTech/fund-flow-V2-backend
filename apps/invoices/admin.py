from django.contrib import admin
from .models import Invoice


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "vendor", "amount", "currency", "status",
        "entry_source", "finance_reference_number", "scope_node", "created_by", "created_at",
    )
    list_filter = ("status", "entry_source", "currency")
    search_fields = ("title", "vendor_invoice_number", "finance_reference_number", "vendor__vendor_name")
    raw_id_fields = (
        "scope_node", "vendor", "created_by", "historical_posted_by", "historical_reversed_by",
    )
