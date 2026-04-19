from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from apps.notifications.models import NotificationDelivery, NotificationChannel, NotificationStatus
from apps.notifications.api.serializers import NotificationDeliverySerializer


class NotificationViewSet(ViewSet):
    """
    V2 Notification Inbox API.

    GET /notifications/               — list current user's in-app notifications
    POST /notifications/{id}/mark-read/  — mark one notification as sent
    POST /notifications/mark-all-read/  — mark all pending as sent
    """
    permission_classes = [IsAuthenticated]

    def list(self, request):
        """
        GET /notifications/
        Query params:
            status — filter by notification status (pending, sent, failed)
        """
        qs = (
            NotificationDelivery.objects
            .filter(
                channel=NotificationChannel.IN_APP,
                event__target_user=request.user,
            )
            .select_related("event", "event__actor_user")
        )

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        qs = qs.order_by("-event__created_at")
        data = NotificationDeliverySerializer(qs, many=True).data
        return Response(data)

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        """
        POST /notifications/{id}/mark-read/
        Marks one notification as sent (only if it belongs to the current user).
        """
        try:
            notification = NotificationDelivery.objects.select_related("event").get(
                pk=pk,
                event__target_user=request.user,
            )
        except NotificationDelivery.DoesNotExist:
            return Response(
                {"detail": "Notification not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if notification.status != NotificationStatus.PENDING:
            return Response(
                {"detail": "Notification is not pending."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        notification.status = NotificationStatus.SENT
        notification.sent_at = timezone.now()
        notification.save(update_fields=["status", "sent_at"])
        return Response(NotificationDeliverySerializer(notification).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        """
        POST /notifications/mark-all-read/
        Marks all pending in-app notifications for the current user as sent.
        """
        updated = NotificationDelivery.objects.filter(
            channel=NotificationChannel.IN_APP,
            event__target_user=request.user,
            status=NotificationStatus.PENDING,
        ).update(
            status=NotificationStatus.SENT,
            sent_at=timezone.now(),
        )
        return Response({"marked_read": updated})
