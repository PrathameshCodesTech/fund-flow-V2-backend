"""
API-level tests for V2 notification inbox endpoints.
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from apps.notifications.models import (
    NotificationDelivery,
    NotificationChannel,
    NotificationStatus,
)
from apps.workflow.models import WorkflowEvent, WorkflowEventType, InstanceStatus
from apps.workflow.models import (
    WorkflowTemplate, WorkflowTemplateVersion, StepGroup, WorkflowStep,
    VersionStatus, ParallelMode, RejectionAction, ScopeResolutionPolicy,
    WorkflowInstance,
)
from apps.users.models import User
from apps.core.models import Organization, ScopeNode, NodeType
from apps.notifications.api.views import NotificationViewSet


@pytest.fixture
def factory():
    return APIRequestFactory()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Notif Org", code="notif-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/notif-org/hq", depth=0,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea",
        node_type=NodeType.ENTITY, path="/notif-org/hq/ea", depth=1,
    )


@pytest.fixture
def actor_user(db):
    return User.objects.create_user(email="actor@notify.com", password="pass")


@pytest.fixture
def target_user(db):
    return User.objects.create_user(email="target@notify.com", password="pass")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@notify.com", password="pass")


@pytest.fixture
def workflow_instance(db, entity, actor_user):
    """Creates a minimal published template + active instance for notification tests."""
    template = WorkflowTemplate.objects.create(
        name="Test WF", module="invoice", scope_node=entity,
        created_by=actor_user,
    )
    version = WorkflowTemplateVersion.objects.create(
        template=template, version_number=1, status=VersionStatus.PUBLISHED,
    )
    group = StepGroup.objects.create(
        template_version=version, name="Group 1", display_order=1,
        parallel_mode=ParallelMode.SINGLE, on_rejection_action=RejectionAction.TERMINATE,
    )
    instance = WorkflowInstance.objects.create(
        template_version=version, subject_type="invoice", subject_id=1,
        subject_scope_node=entity, status=InstanceStatus.ACTIVE,
    )
    return instance


@pytest.fixture
def pending_notification(workflow_instance, actor_user, target_user):
    """A PENDING in_app notification for target_user."""
    event = WorkflowEvent.objects.create(
        instance=workflow_instance,
        event_type=WorkflowEventType.STEP_ASSIGNED,
        actor_user=actor_user,
        target_user=target_user,
        metadata={"instance_step_id": 1},
    )
    return NotificationDelivery.objects.create(
        event=event,
        channel=NotificationChannel.IN_APP,
        status=NotificationStatus.PENDING,
    )


@pytest.fixture
def sent_notification(workflow_instance, actor_user, target_user):
    """A SENT in_app notification for target_user."""
    event = WorkflowEvent.objects.create(
        instance=workflow_instance,
        event_type=WorkflowEventType.STEP_ASSIGNED,
        actor_user=actor_user,
        target_user=target_user,
        metadata={"instance_step_id": 2},
    )
    return NotificationDelivery.objects.create(
        event=event,
        channel=NotificationChannel.IN_APP,
        status=NotificationStatus.SENT,
        sent_at=timezone.now(),
    )


def _make_request(factory, method, path, user, data=None):
    fn = getattr(factory, method)
    request = fn(path, data, format="json") if data else fn(path)
    force_authenticate(request, user=user)
    return request


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNotificationList:
    def test_user_sees_only_own_notifications(
        self, factory, pending_notification, target_user, other_user,
    ):
        """
        GET /notifications/ returns only notifications where target_user = request.user.
        """
        request = _make_request(factory, "get", "/notifications/", target_user)
        view = NotificationViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200
        ids = [n["id"] for n in response.data]
        assert pending_notification.pk in ids
        # other_user should not see it
        request2 = _make_request(factory, "get", "/notifications/", other_user)
        response2 = view(request2)
        assert pending_notification.pk not in [n["id"] for n in response2.data]

    def test_pending_filter_works(
        self, factory, pending_notification, sent_notification, target_user,
    ):
        """GET /notifications/?status=pending returns only pending."""
        request = _make_request(
            factory, "get", "/notifications/?status=pending", target_user
        )
        view = NotificationViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200
        ids = [n["id"] for n in response.data]
        assert pending_notification.pk in ids
        assert sent_notification.pk not in ids

    def test_sent_filter_works(
        self, factory, pending_notification, sent_notification, target_user,
    ):
        """GET /notifications/?status=sent returns only sent."""
        request = _make_request(
            factory, "get", "/notifications/?status=sent", target_user
        )
        view = NotificationViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200
        ids = [n["id"] for n in response.data]
        assert sent_notification.pk in ids
        assert pending_notification.pk not in ids

    def test_only_in_app_channel_returned(
        self, factory, workflow_instance, actor_user, target_user,
    ):
        """Non in_app notifications are never returned."""
        event = WorkflowEvent.objects.create(
            instance=workflow_instance,
            event_type=WorkflowEventType.STEP_ASSIGNED,
            actor_user=actor_user,
            target_user=target_user,
        )
        # Email channel — should not appear
        NotificationDelivery.objects.create(
            event=event,
            channel=NotificationChannel.EMAIL,
            status=NotificationStatus.PENDING,
        )
        request = _make_request(factory, "get", "/notifications/", target_user)
        view = NotificationViewSet.as_view({"get": "list"})
        response = view(request)
        assert response.status_code == 200
        # Email notification should not be in the list
        assert all(n["channel"] == "in_app" for n in response.data)


class TestMarkRead:
    def test_mark_read_succeeds_for_own_notification(
        self, factory, pending_notification, target_user,
    ):
        """POST /notifications/{id}/mark-read/ marks the notification as sent."""
        request = _make_request(
            factory, "post",
            f"/notifications/{pending_notification.pk}/mark-read/",
            target_user,
        )
        view = NotificationViewSet.as_view({"post": "mark_read"})
        response = view(request, pk=pending_notification.pk)
        assert response.status_code == 200
        assert response.data["status"] == NotificationStatus.SENT
        assert response.data["sent_at"] is not None

    def test_cannot_mark_another_users_notification(
        self, factory, pending_notification, other_user,
    ):
        """Marking a notification belonging to a different user returns 404."""
        request = _make_request(
            factory, "post",
            f"/notifications/{pending_notification.pk}/mark-read/",
            other_user,
        )
        view = NotificationViewSet.as_view({"post": "mark_read"})
        response = view(request, pk=pending_notification.pk)
        assert response.status_code == 404

    def test_mark_read_idempotent_when_already_sent(
        self, factory, sent_notification, target_user,
    ):
        """Marking an already-sent notification returns 400."""
        request = _make_request(
            factory, "post",
            f"/notifications/{sent_notification.pk}/mark-read/",
            target_user,
        )
        view = NotificationViewSet.as_view({"post": "mark_read"})
        response = view(request, pk=sent_notification.pk)
        assert response.status_code == 400
        assert "not pending" in response.data["detail"]


class TestMarkAllRead:
    def test_mark_all_read_marks_only_own_pending(
        self, factory, workflow_instance, actor_user, target_user,
    ):
        """
        POST /notifications/mark-all-read/ marks all pending in_app
        notifications for the current user.
        """
        # Create two pending notifications for target_user
        event1 = WorkflowEvent.objects.create(
            instance=workflow_instance, event_type=WorkflowEventType.STEP_ASSIGNED,
            actor_user=actor_user, target_user=target_user,
        )
        event2 = WorkflowEvent.objects.create(
            instance=workflow_instance, event_type=WorkflowEventType.STEP_APPROVED,
            actor_user=actor_user, target_user=target_user,
        )
        notif1 = NotificationDelivery.objects.create(
            event=event1, channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
        )
        notif2 = NotificationDelivery.objects.create(
            event=event2, channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
        )

        # Mark all read
        request = _make_request(
            factory, "post", "/notifications/mark-all-read/", target_user,
        )
        view = NotificationViewSet.as_view({"post": "mark_all_read"})
        response = view(request)
        assert response.status_code == 200
        assert response.data["marked_read"] == 2

        # Both should be marked sent
        notif1.refresh_from_db()
        notif2.refresh_from_db()
        assert notif1.status == NotificationStatus.SENT
        assert notif2.status == NotificationStatus.SENT

    def test_mark_all_read_does_not_affect_other_users(
        self, factory, workflow_instance, actor_user, target_user, other_user,
    ):
        """Marking all read for user A does not affect user B's notifications."""
        event = WorkflowEvent.objects.create(
            instance=workflow_instance, event_type=WorkflowEventType.STEP_ASSIGNED,
            actor_user=actor_user, target_user=target_user,
        )
        target_notif = NotificationDelivery.objects.create(
            event=event, channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
        )

        event_other = WorkflowEvent.objects.create(
            instance=workflow_instance, event_type=WorkflowEventType.STEP_ASSIGNED,
            actor_user=actor_user, target_user=other_user,
        )
        other_notif = NotificationDelivery.objects.create(
            event=event_other, channel=NotificationChannel.IN_APP,
            status=NotificationStatus.PENDING,
        )

        request = _make_request(
            factory, "post", "/notifications/mark-all-read/", target_user,
        )
        view = NotificationViewSet.as_view({"post": "mark_all_read"})
        response = view(request)
        assert response.data["marked_read"] == 1

        other_notif.refresh_from_db()
        assert other_notif.status == NotificationStatus.PENDING  # unaffected
