from django.contrib import admin
from .models import NotificationDelivery


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "channel", "status", "sent_at", "created_at")
    list_filter = ("channel", "status")
    raw_id_fields = ("event",)
