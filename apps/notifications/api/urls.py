from django.urls import path
from apps.notifications.api.views import NotificationViewSet

notification_list = NotificationViewSet.as_view({"get": "list"})
notification_mark_read = NotificationViewSet.as_view({"post": "mark_read"})
notification_mark_all_read = NotificationViewSet.as_view({"post": "mark_all_read"})

urlpatterns = [
    path("", notification_list, name="notification-list"),
    path("<int:pk>/mark-read/", notification_mark_read, name="notification-mark-read"),
    path("mark-all-read/", notification_mark_all_read, name="notification-mark-all-read"),
]
