from django.db import models


class NotificationChannel(models.TextChoices):
    IN_APP = "in_app", "In App"
    EMAIL = "email", "Email"
    SLACK = "slack", "Slack"


class NotificationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class NotificationDelivery(models.Model):
    """
    Tracks delivery of a WorkflowEvent to a user via a specific channel.
    Events = business logic (in workflow app).
    Delivery = channel detail (here).

    V1 delivers in_app only. email/slack added as channels without new event logic.
    """
    event = models.ForeignKey(
        "workflow.WorkflowEvent",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    channel = models.CharField(max_length=20, choices=NotificationChannel.choices)
    status = models.CharField(
        max_length=20,
        choices=NotificationStatus.choices,
        default=NotificationStatus.PENDING,
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notification_deliveries"
        indexes = [
            models.Index(fields=["event", "channel"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Delivery [{self.channel}] {self.status} for Event {self.event_id}"
