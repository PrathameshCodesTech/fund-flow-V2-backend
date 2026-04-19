from rest_framework import serializers
from apps.notifications.models import NotificationDelivery, NotificationChannel


class NotificationDeliverySerializer(serializers.ModelSerializer):
    event_id = serializers.IntegerField(source="event.id", read_only=True)
    event_type = serializers.CharField(source="event.event_type", read_only=True)
    actor_user_id = serializers.IntegerField(source="event.actor_user_id", read_only=True, allow_null=True)
    actor_user_email = serializers.CharField(source="event.actor_user.email", read_only=True, allow_null=True)
    target_user_id = serializers.IntegerField(source="event.target_user_id", read_only=True, allow_null=True)
    instance_id = serializers.IntegerField(source="event.instance_id", read_only=True)
    metadata = serializers.JSONField(source="event.metadata", read_only=True)
    created_at = serializers.DateTimeField(source="event.created_at", read_only=True)

    class Meta:
        model = NotificationDelivery
        fields = (
            "id",
            "channel",
            "status",
            "sent_at",
            "created_at",
            "event_id",
            "event_type",
            "actor_user_id",
            "actor_user_email",
            "target_user_id",
            "instance_id",
            "metadata",
        )
        read_only_fields = fields
