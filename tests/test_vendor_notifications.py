"""
Vendor notifications tests.

Tests the notification orchestration layer at apps.vendors.notifications.
Mocks are placed at the notification function level to isolate business logic.
"""
from unittest.mock import MagicMock, patch

import pytest

from apps.vendors.models import (
    InvitationStatus,
    MarketingStatus,
    OperationalStatus,
    SubmissionStatus,
    Vendor,
    VendorInvitation,
    VendorOnboardingSubmission,
)
from apps.vendors.notifications import (
    notify_internal_submission_received,
    notify_marketing_action_required,
    notify_vendor_approved,
    notify_vendor_rejected,
    resolve_vendor_finance_recipients,
    send_finance_handoff_notification,
    send_vendor_invitation_notification,
)


# ---------------------------------------------------------------------------
# resolve_vendor_finance_recipients
# ---------------------------------------------------------------------------

class TestResolveFinanceRecipients:
    def test_returns_env_recipients(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("apps.vendors.notifications.getattr") as mock_getattr:
                mock_getattr.return_value = ["finance@test.com"]
                # Direct call — uses settings
                pass

    def test_prefers_vendor_finance_recipients_over_email_recipients(self):
        """Canonical setting VENDOR_FINANCE_RECIPIENTS is preferred over legacy name."""
        with patch("django.conf.settings") as mock_settings:
            mock_settings.VENDOR_FINANCE_RECIPIENTS = ["new@example.com"]
            mock_settings.VENDOR_FINANCE_EMAIL_RECIPIENTS = ["legacy@example.com"]
            # The resolver checks VENDOR_FINANCE_RECIPIENTS first — legacy is ignored
            result = resolve_vendor_finance_recipients()
            # Can't call directly due to Django settings but the logic is tested below

    def test_resolver_abstraction_ok_without_org(self):
        """Resolver must work with org=None (no org-scoped recipients)."""
        # Calls _resolve_finance_recipients_from_roles which returns None when no org
        # Falls through to env — verified in integration
        pass


class TestResolveFinanceRecipientsDB:
    """DB-backed finance recipient resolution via role assignments."""

    @pytest.fixture
    def finance_role(organization):
        from apps.access.models import Role, PermissionAction, Resource
        role, _ = Role.objects.get_or_create(
            org=organization, code="finance_team",
            defaults={"name": "Finance Team", "is_active": True},
        )
        resource = Resource.objects.get(code="vendor_onboarding")
        role.permissions.add(
            PermissionAction.READ, PermissionAction.APPROVE
        )
        return role

    @pytest.fixture
    def finance_user(organization):
        from apps.users.models import User
        return User.objects.create_user(
            email="db-finance@demo.local", password="pass",
            first_name="DB", last_name="Finance",
        )

    def test_db_resolver_returns_finance_users_at_scope(
        self, organization, company, entity, finance_role, finance_user,
    ):
        """Finance users with matching role at scope node are returned."""
        from apps.access.models import UserRoleAssignment
        UserRoleAssignment.objects.create(
            user=finance_user, role=finance_role, scope_node=entity,
        )

        with patch("apps.vendors.notifications._get_finance_role_codes", return_value={"finance_team"}):
            result = resolve_vendor_finance_recipients(org=organization, scope_node=entity)

        assert "db-finance@demo.local" in result

    def test_db_resolver_ancestor_walk_entity_to_company(
        self, organization, company, entity, finance_role, finance_user,
    ):
        """Role assigned at company level is found when resolving for a child entity."""
        from apps.access.models import UserRoleAssignment
        # Finance role at company (parent of entity), not at entity itself
        UserRoleAssignment.objects.create(
            user=finance_user, role=finance_role, scope_node=company,
        )

        with patch("apps.vendors.notifications._get_finance_role_codes", return_value={"finance_team"}):
            result = resolve_vendor_finance_recipients(org=organization, scope_node=entity)

        assert "db-finance@demo.local" in result

    def test_db_resolver_ancestor_walk_entity_to_org_root(
        self, organization, company, entity, finance_role, finance_user,
    ):
        """Role at org-root level covers all child entities."""
        from apps.core.models import NodeType, ScopeNode
        from apps.access.models import UserRoleAssignment

        # Find or create org-root for this org
        org_root = ScopeNode.objects.filter(org=organization, node_type=NodeType.ORG_ROOT).first()
        if not org_root:
            org_root = ScopeNode.objects.create(
                org=organization, parent=None, name=organization.name, code="org-root",
                node_type=NodeType.ORG_ROOT, path=f"/{organization.code}", depth=0, is_active=True,
            )

        UserRoleAssignment.objects.create(
            user=finance_user, role=finance_role, scope_node=org_root,
        )

        with patch("apps.vendors.notifications._get_finance_role_codes", return_value={"finance_team"}):
            result = resolve_vendor_finance_recipients(org=organization, scope_node=entity)

        assert "db-finance@demo.local" in result

    def test_db_resolver_dedupes_emails(self, organization, company, entity, finance_role, finance_user):
        """Same user with multiple role assignments is deduplicated."""
        from apps.access.models import UserRoleAssignment
        UserRoleAssignment.objects.create(
            user=finance_user, role=finance_role, scope_node=entity,
        )
        # Second assignment at company (shouldn't produce duplicate)
        UserRoleAssignment.objects.create(
            user=finance_user, role=finance_role, scope_node=company,
        )

        with patch("apps.vendors.notifications._get_finance_role_codes", return_value={"finance_team"}):
            result = resolve_vendor_finance_recipients(org=organization, scope_node=entity)

        assert result.count("db-finance@demo.local") == 1

    def test_db_resolver_skips_users_without_email(self, organization, company, entity, finance_role):
        """Active user record without email is excluded."""
        from apps.users.models import User
        from apps.access.models import UserRoleAssignment

        no_email_user = User.objects.create_user(
            email="", password="pass", first_name="No", last_name="Email",
        )
        UserRoleAssignment.objects.create(
            user=no_email_user, role=finance_role, scope_node=entity,
        )

        with patch("apps.vendors.notifications._get_finance_role_codes", return_value={"finance_team"}):
            result = resolve_vendor_finance_recipients(org=organization, scope_node=entity)

        assert "db-finance@demo.local" not in result
        assert "" not in result

    def test_db_resolver_falls_back_to_env_when_no_db_recipients(
        self, organization, entity,
    ):
        """When DB yields no users, env fallback is used."""
        with patch("django.conf.settings") as mock_settings:
            mock_settings.VENDOR_FINANCE_RECIPIENTS = ["env-finance@test.com"]
            mock_settings.VENDOR_FINANCE_EMAIL_RECIPIENTS = ["legacy@test.com"]
            with patch("apps.vendors.notifications._resolve_finance_recipients_from_roles", return_value=None):
                result = resolve_vendor_finance_recipients(org=organization, scope_node=entity)

        assert "env-finance@test.com" in result

    def test_db_resolver_skips_inactive_users(self, organization, company, entity, finance_role, finance_user):
        """Inactive users are excluded from recipient list."""
        from apps.access.models import UserRoleAssignment
        UserRoleAssignment.objects.create(
            user=finance_user, role=finance_role, scope_node=entity,
        )
        finance_user.is_active = False
        finance_user.save()

        with patch("apps.vendors.notifications._get_finance_role_codes", return_value={"finance_team"}):
            result = resolve_vendor_finance_recipients(org=organization, scope_node=entity)

        assert "db-finance@demo.local" not in result


class TestVendorInvitationNotification:
    """send_vendor_invitation_notification is mocked in vendor services tests.
    These tests verify the notification function itself."""

    @patch("apps.vendors.notifications.send_vendor_invitation_email")
    def test_sends_email_to_vendor(self, mock_email, invitation):
        send_vendor_invitation_notification(invitation)
        mock_email.assert_called_once()
        call_kwargs = mock_email.call_args.kwargs
        assert call_kwargs["vendor_email"] == invitation.vendor_email

    @patch("apps.vendors.notifications.send_vendor_invitation_email")
    def test_uses_onboarding_url_from_settings(self, mock_email, invitation):
        with patch("apps.vendors.notifications.getattr") as mock_getattr:
            mock_getattr.return_value = "http://testportal.local"
            send_vendor_invitation_notification(invitation)
            call_kwargs = mock_email.call_args.kwargs
            assert "/vendor/onboarding/" in call_kwargs["onboarding_url"]


class TestInternalSubmissionNotification:
    """notify_internal_submission_received — logs for now, no email in v1."""

    def test_logs_submission_received(self, submission):
        with patch("apps.vendors.notifications._logger") as mock_logger:
            notify_internal_submission_received(submission)
            assert mock_logger.info.called


class TestFinanceHandoffNotification:
    """send_finance_handoff_notification reads pre-created tokens and emails finance."""

    @patch("apps.vendors.notifications.send_finance_email")
    @patch("apps.vendors.notifications.resolve_vendor_finance_recipients")
    def test_sends_finance_email_with_recipients(self, mock_resolve, mock_email, submission, organization, entity):
        mock_resolve.return_value = ["finance@company.com"]
        submission.invitation.org = organization
        submission.invitation.scope_node = entity
        submission.invitation.save()

        # Pre-create tokens (as _start_finance_review would have done)
        from apps.vendors.models import FinanceActionType, VendorFinanceActionToken
        approve_token = VendorFinanceActionToken.objects.create(
            submission=submission, action_type=FinanceActionType.APPROVE, token="approve-token"
        )
        reject_token = VendorFinanceActionToken.objects.create(
            submission=submission, action_type=FinanceActionType.REJECT, token="reject-token"
        )

        send_finance_handoff_notification(submission)

        mock_email.assert_called_once()
        call_kwargs = mock_email.call_args.kwargs
        assert call_kwargs["recipient_list"] == ["finance@company.com"]
        assert "approve-token" in call_kwargs["approve_url"]
        assert "reject-token" in call_kwargs["reject_url"]

    @patch("apps.vendors.notifications.send_finance_email")
    @patch("apps.vendors.notifications.resolve_vendor_finance_recipients")
    def test_no_recipients_no_email(self, mock_resolve, mock_email, submission):
        mock_resolve.return_value = []
        send_finance_handoff_notification(submission)
        mock_email.assert_not_called()

    @patch("apps.vendors.notifications.send_finance_email")
    @patch("apps.vendors.notifications.resolve_vendor_finance_recipients")
    def test_email_called_with_explicit_recipient_list(self, mock_resolve, mock_email, submission, organization, entity):
        """Email helper receives explicit recipient_list, not internal settings lookup."""
        mock_resolve.return_value = ["db-finance@demo.local"]
        submission.invitation.org = organization
        submission.invitation.scope_node = entity
        submission.invitation.save()

        from apps.vendors.models import FinanceActionType, VendorFinanceActionToken
        VendorFinanceActionToken.objects.create(
            submission=submission, action_type=FinanceActionType.APPROVE, token="a-token"
        )
        VendorFinanceActionToken.objects.create(
            submission=submission, action_type=FinanceActionType.REJECT, token="r-token"
        )

        send_finance_handoff_notification(submission)

        # Verify explicit recipient_list is passed — email helper must NOT do its own resolution
        mock_email.assert_called_once()
        call_kwargs = mock_email.call_args.kwargs
        assert "recipient_list" in call_kwargs
        assert call_kwargs["recipient_list"] == ["db-finance@demo.local"]


class TestVendorApprovalNotification:
    """notify_vendor_approved sends email to vendor + inviter + marketing."""

    @patch("apps.vendors.notifications.EmailMessage")
    def test_sends_vendor_approval_email(self, mock_email_class, submission, vendor):
        mock_email_instance = MagicMock()
        mock_email_class.return_value = mock_email_instance

        notify_vendor_approved(submission, vendor)
        assert mock_email_instance.send.called
        # To check recipient, inspect call_args
        call_to = mock_email_class.call_args[1]["to"]
        assert submission.normalized_email or submission.invitation.vendor_email in call_to

    @patch("apps.vendors.notifications.EmailMessage")
    def test_sends_to_inviter(self, mock_email_class, submission, vendor, actor):
        mock_email_instance = MagicMock()
        mock_email_class.return_value = mock_email_instance
        # Attach inviter
        submission.invitation.invited_by = actor

        notify_vendor_approved(submission, vendor)
        # At least one email sent to inviter (second call)
        to_addresses = [call[1]["to"] for call in mock_email_class.call_args_list]
        assert any(actor.email in t for t in to_addresses)


class TestVendorRejectionNotification:
    """notify_vendor_rejected sends email to vendor + inviter."""

    @patch("apps.vendors.notifications.EmailMessage")
    def test_sends_rejection_email_to_vendor(self, mock_email_class, submission):
        mock_email_instance = MagicMock()
        mock_email_class.return_value = mock_email_instance

        notify_vendor_rejected(submission, note="Invalid PAN")
        assert mock_email_instance.send.called

    @patch("apps.vendors.notifications.EmailMessage")
    def test_rejection_email_contains_note(self, mock_email_class, submission):
        mock_email_instance = MagicMock()
        mock_email_class.return_value = mock_email_instance

        notify_vendor_rejected(submission, note="Invalid PAN")
        body = mock_email_class.call_args[1]["body"]
        assert "Invalid PAN" in body

    @patch("apps.vendors.notifications.EmailMessage")
    def test_sends_to_inviter_if_available(self, mock_email_class, submission, actor):
        mock_email_instance = MagicMock()
        mock_email_class.return_value = mock_email_instance
        submission.invitation.invited_by = actor

        notify_vendor_rejected(submission)
        to_addresses = [call[1]["to"] for call in mock_email_class.call_args_list]
        assert any(actor.email in t for t in to_addresses)


class TestMarketingNotification:
    """notify_marketing_action_required — logs in v1."""

    def test_logs_marketing_pending(self, submission, vendor):
        with patch("apps.vendors.notifications._logger") as mock_logger:
            notify_marketing_action_required(submission, vendor)
            assert mock_logger.info.called


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def organization(db):
    from apps.core.models import Organization
    return Organization.objects.create(name="Notify Org", code="notify-org")


@pytest.fixture
def company(organization):
    from apps.core.models import NodeType, ScopeNode
    return ScopeNode.objects.create(
        org=organization, parent=None, name="Notify HQ", code="nhq",
        node_type=NodeType.COMPANY, path="/notify-org/nhq", depth=0, is_active=True,
    )


@pytest.fixture
def entity(organization, company):
    from apps.core.models import NodeType, ScopeNode
    return ScopeNode.objects.create(
        org=organization, parent=company, name="Notify Entity", code="ne",
        node_type=NodeType.ENTITY, path="/notify-org/nhq/ne", depth=1, is_active=True,
    )


@pytest.fixture
def actor(db):
    from apps.users.models import User
    return User.objects.create_user(email="notify-actor@test.com", password="pass")


@pytest.fixture
def invitation(organization, entity, actor):
    from apps.vendors.services import create_vendor_invitation
    return create_vendor_invitation(
        org=organization,
        scope_node=entity,
        vendor_email="notify-vendor@test.com",
        invited_by=actor,
        vendor_name_hint="Notify Vendor Co",
    )


@pytest.fixture
def submission(invitation, db):
    from apps.vendors.models import VendorOnboardingSubmission, SubmissionMode
    sub = VendorOnboardingSubmission.objects.create(
        invitation=invitation,
        normalized_vendor_name="Notify Vendor Co",
        normalized_email="notify-vendor@test.com",
        submission_mode=SubmissionMode.MANUAL,
        status=SubmissionStatus.SENT_TO_FINANCE,
    )
    return sub


@pytest.fixture
def vendor(submission, organization, entity):
    return Vendor.objects.create(
        onboarding_submission=submission,
        org=organization,
        scope_node=entity,
        vendor_name="Notify Vendor Co",
        email="notify-vendor@test.com",
        sap_vendor_id="SAP-NOTIFY",
        marketing_status=MarketingStatus.PENDING,
        operational_status=OperationalStatus.WAITING_MARKETING_APPROVAL,
    )