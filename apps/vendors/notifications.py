"""
Vendor notification orchestration.

Single place that owns all vendor lifecycle notifications:
  - invitation email to vendor
  - internal admin notifications on vendor submit
  - finance handoff email on vendor finalize
  - vendor/internal notifications on finance approve/reject
  - marketing activation notifications

All functions are mockable in tests via the standard patch path:
  apps.vendors.notifications.send_vendor_invitation_notification
  apps.vendors.notifications.notify_internal_submission_received
  apps.vendors.notifications.send_finance_handoff_notification
  apps.vendors.notifications.notify_vendor_approved
  apps.vendors.notifications.notify_vendor_rejected
  apps.vendors.notifications.notify_marketing_action_required
"""

import logging
from typing import TYPE_CHECKING
from django.conf import settings

if TYPE_CHECKING:
    from apps.vendors.models import VendorInvitation, VendorOnboardingSubmission, Vendor

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Finance role configuration
# ---------------------------------------------------------------------------

def _get_finance_role_codes():
    """
    Return the set of role codes that qualify as finance recipients.

    Override via settings.FINANCE_ROLE_CODES (set of strings).
    Falls back to {"finance_team"} for backwards compatibility.
    """
    from django.conf import settings
    return set(getattr(settings, "FINANCE_ROLE_CODES", {"finance_team"}))


# ---------------------------------------------------------------------------
# Finance recipient resolver
# ---------------------------------------------------------------------------

def resolve_vendor_finance_recipients(org=None, scope_node=None):
    """
    Resolve finance recipient emails for a vendor submission.

    Resolution order:
        1. DB-backed: users with finance role assignments at the submission's
           scope node, company level, or org root level.
        2. Env fallback: VENDOR_FINANCE_RECIPIENTS / VENDOR_FINANCE_EMAIL_RECIPIENTS

    Ancestor walk-up is intentional: finance assignments at company or org-root
    cover all child entities automatically.

    Args:
        org:        Organization instance (from submission.invitation.org)
        scope_node: ScopeNode instance (from submission.invitation.scope_node)

    Returns:
        list[str] of email addresses. Empty list if none configured.
    """
    db_recipients = _resolve_finance_recipients_from_roles(org, scope_node)
    if db_recipients:
        _logger.debug(
            "Finance recipients resolved from DB for org=%s, scope=%s: %s",
            org, scope_node, db_recipients,
        )
        return db_recipients

    # Fallback to env config
    from django.conf import settings
    fallback = getattr(
        settings,
        "VENDOR_FINANCE_RECIPIENTS",
        getattr(settings, "VENDOR_FINANCE_EMAIL_RECIPIENTS", []),
    )
    if isinstance(fallback, str):
        fallback = [fallback]
    result = list(fallback) if fallback else []
    if result:
        _logger.debug(
            "Finance recipients using env fallback for org=%s, scope=%s: %s",
            org, scope_node, result,
        )
    return result


def _resolve_finance_recipients_from_roles(org, scope_node):
    """
    Query DB for users with finance role assignments at scope_node or its ancestors.

    Returns list of email addresses, or None if org is required but not provided.

    Ancestor resolution: if scope_node is an entity, also check its company and
    org-root ancestors for finance role assignments. Finance roles at higher levels
    apply to all child scope nodes.
    """
    if not org:
        return None

    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Collect scope IDs to check: scope_node + all ancestor paths
    scope_ids = _get_scope_ids_for_ancestor_walk(org, scope_node)
    if not scope_ids:
        return None

    role_codes = _get_finance_role_codes()
    if not role_codes:
        return None

    # Find users with matching finance roles at any of the collected scope IDs
    users = User.objects.filter(
        role_assignments__role__org=org,
        role_assignments__role__code__in=role_codes,
        role_assignments__scope_node_id__in=scope_ids,
        is_active=True,
    ).exclude(email="").exclude(email__isnull=True).distinct()

    emails = [u.email.strip() for u in users if u.email]
    return emails if emails else None


def _get_scope_ids_for_ancestor_walk(org, scope_node):
    """
    Return list of scope_node IDs to check for finance role assignments.

    Includes:
    - The scope_node itself (entity level)
    - Its company ancestor (parent of entity)
    - Org-root ancestor (top of hierarchy)

    This implements the ancestor-walk design for finance role resolution.
    """
    if not scope_node:
        # No scope given — fall back to all company-level + org-root scopes for this org
        from apps.core.models import ScopeNode, NodeType
        return list(
            ScopeNode.objects.filter(org=org, node_type__in=[NodeType.COMPANY, NodeType.ORG_ROOT])
            .values_list("id", flat=True)
        )

    # Build ancestor ID set from path
    # Path format: /org_code/company_code/entity_code/...
    path = scope_node.path
    parts = path.strip("/").split("/")

    # Generate all ancestor paths (each prefix is an ancestor)
    ancestor_paths = []
    for i in range(1, len(parts)):
        ancestor_paths.append("/" + "/".join(parts[:i]) + "/")

    if not ancestor_paths:
        return [scope_node.id]

    from apps.core.models import ScopeNode
    ancestor_ids = list(
        ScopeNode.objects.filter(org=org, path__in=ancestor_paths)
        .values_list("id", flat=True)
    )
    return ancestor_ids + [scope_node.id]


# ---------------------------------------------------------------------------
# 1. Vendor invitation notification
# ---------------------------------------------------------------------------

def send_vendor_invitation_notification(invitation: "VendorInvitation") -> None:
    """
    Send the vendor onboarding invitation email.

    Args:
        invitation: VendorInvitation instance

    Raises:
        Logs and propagates no exception — caller decides how to handle failure.
    """
    try:
        from apps.vendors.email import send_vendor_invitation_email
        from django.conf import settings

        portal_base = getattr(settings, "VENDOR_PORTAL_BASE_URL", "http://localhost:5173")
        onboarding_url = f"{portal_base}/vendor/onboarding/{invitation.token}"

        invited_by = getattr(invitation, "invited_by", None)
        if invited_by:
            invited_by_name = invited_by.get_full_name().strip() or invited_by.email
        else:
            invited_by_name = "Fund Flow"

        send_vendor_invitation_email(
            vendor_email=invitation.vendor_email,
            vendor_name_hint=invitation.vendor_name_hint,
            onboarding_url=onboarding_url,
            invited_by_name=invited_by_name,
        )
        _logger.info(
            "Vendor invitation email sent for invitation_id=%s to %s",
            invitation.pk, invitation.vendor_email,
        )
    except Exception as exc:
        _logger.warning(
            "Failed to send vendor invitation email for %s (invitation_id=%s): %s",
            invitation.vendor_email, invitation.pk, exc,
        )


# ---------------------------------------------------------------------------
# 2. Internal admin notification — vendor submitted and auto-sent to finance
# ---------------------------------------------------------------------------

def notify_internal_submission_received(submission: "VendorOnboardingSubmission") -> None:
    """
    Notify internal admin/inviter that a vendor submission was received and
    has entered finance review automatically.

    Currently logs for visibility. In a full implementation this would also
    send an in-app notification or email to the inviter.

    Args:
        submission: VendorOnboardingSubmission in sent_to_finance state
    """
    try:
        invitation = submission.invitation
        inviter = getattr(invitation, "invited_by", None)
        vendor_name = submission.normalized_vendor_name or invitation.vendor_name_hint or "Unknown"
        vendor_email = submission.normalized_email or invitation.vendor_email

        # Log for now — acts as structured audit trail
        inviter_info = (
            f"{inviter.get_full_name()} ({inviter.email})"
            if inviter else "system"
        )
        _logger.info(
            "[VendorOnboarding] Submission received and sent to finance. "
            "vendor=%s (%s), submission_id=%s, inviter=%s, status=%s",
            vendor_name, vendor_email, submission.pk,
            inviter_info, submission.status,
        )

        # TODO: wire to in-app notification system when available
        # e.g., Notification.objects.create(recipient=inviter, ...)

    except Exception as exc:
        _logger.warning(
            "Failed to notify internal user of submission received. submission_id=%s: %s",
            submission.pk, exc,
        )


# ---------------------------------------------------------------------------
# 3. Finance handoff notification (email to finance team)
# ---------------------------------------------------------------------------

def send_finance_handoff_notification(submission: "VendorOnboardingSubmission") -> None:
    """
    Send the VRF package email to finance recipients.

    Tokens are created in _start_finance_review (services.py). This function
    only sends the communication email. It assumes tokens already exist on
    the submission and reads them to build approve/reject URLs.

    Args:
        submission: VendorOnboardingSubmission in sent_to_finance state
    """
    try:
        from apps.vendors.email import send_finance_email
        from apps.vendors.models import FinanceActionType

        # Tokens already created by _start_finance_review — look them up
        tokens = list(submission.finance_tokens.all())
        approve_token_record = next(
            (t for t in tokens if t.action_type == FinanceActionType.APPROVE), None
        )
        reject_token_record = next(
            (t for t in tokens if t.action_type == FinanceActionType.REJECT), None
        )

        if not approve_token_record:
            _logger.warning(
                "Approve finance token missing for submission_id=%s — cannot build review URL",
                submission.pk,
            )
            return

        if not reject_token_record:
            _logger.warning(
                "Reject finance token missing for submission_id=%s — cannot build reject URL",
                submission.pk,
            )
            return

        base_url = getattr(
            settings,
            "VENDOR_FINANCE_PORTAL_BASE_URL",
            getattr(settings, "VENDOR_PORTAL_BASE_URL", "http://localhost:3000"),
        )
        # Both buttons open the approve-token page; ?action= pre-selects the form.
        approve_url = f"{base_url}/vendor/finance/{approve_token_record.token}?action=approve"
        reject_url = f"{base_url}/vendor/finance/{approve_token_record.token}?action=reject"

        # Attachment URLs
        attachment_urls = list(
            submission.attachments.exclude(file_url="").values_list("file_url", flat=True)
        )

        # Resolve inviting user and scope for email context
        invitation = submission.invitation
        inviting_user = None
        scope_name = None
        if invitation:
            inviter = getattr(invitation, "invited_by", None)
            if inviter:
                inviting_user = inviter.get_full_name().strip() or inviter.email
            if invitation.scope_node:
                scope_name = getattr(invitation.scope_node, "name", None)

        # Recipients via resolver
        recipients = resolve_vendor_finance_recipients(
            org=invitation.org if invitation else None,
            scope_node=invitation.scope_node if invitation else None,
        )

        if not recipients:
            _logger.warning(
                "No finance recipients configured for submission_id=%s — skipping handoff email",
                submission.pk,
            )
            return

        send_finance_email(
            submission_id=submission.pk,
            vendor_name=submission.normalized_vendor_name or "Unknown Vendor",
            approve_url=approve_url,
            reject_url=reject_url,
            inviting_user=inviting_user,
            scope_name=scope_name,
            exported_excel_path=submission.exported_excel_file or None,
            attachment_urls=attachment_urls,
            recipient_list=recipients,
        )
        _logger.info(
            "Finance handoff email sent for submission_id=%s to %s",
            submission.pk, recipients,
        )
    except Exception as exc:
        _logger.error(
            "Failed to send finance handoff email for submission_id=%s: %s",
            submission.pk, exc,
        )
        # Re-raise so the caller transaction can decide what to do
        raise


# ---------------------------------------------------------------------------
# 4. Vendor + internal notification on finance approval
# ---------------------------------------------------------------------------

def notify_vendor_approved(submission: "VendorOnboardingSubmission", vendor: "Vendor") -> None:
    """
    Notify vendor and internal users that finance approved the submission.

    Args:
        submission: VendorOnboardingSubmission (now in marketing_pending)
        vendor:      Vendor instance
    """
    try:
        from django.conf import settings
        from django.core.mail import EmailMessage

        invitation = submission.invitation
        vendor_email = submission.normalized_email or invitation.vendor_email
        vendor_name = submission.normalized_vendor_name or "your registration"
        inviter = getattr(invitation, "invited_by", None)

        # ── Vendor email ──────────────────────────────────────────────────
        subject = "Fund Flow — Vendor Onboarding Approved"
        body = (
            f"Dear Vendor,\n\n"
            f"Your vendor registration on Fund Flow has been reviewed and approved by our finance team.\n\n"
            f"Vendor Name : {vendor_name}\n"
            f"SAP Vendor ID: {vendor.sap_vendor_id or 'Pending assignment'}\n\n"
            f"The next step is marketing review. You will be notified once that is complete.\n\n"
            f"If you have questions, please contact your representative.\n\n"
            f"Regards,\nThe Fund Flow Team"
        )
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[vendor_email],
        )
        email.send(fail_silently=False)
        _logger.info("Vendor approval email sent for submission_id=%s to %s", submission.pk, vendor_email)

        # ── Internal inviter notification ─────────────────────────────────
        if inviter and inviter.email:
            try:
                inviter_subject = f"[Fund Flow] Vendor Registration Approved — {vendor_name}"
                inviter_body = (
                    f"A vendor registration has been approved by finance.\n\n"
                    f"Vendor  : {vendor_name}\n"
                    f"Email   : {vendor_email}\n"
                    f"SAP ID  : {vendor.sap_vendor_id or 'Pending'}\n"
                    f"Submission: #{submission.pk}\n\n"
                    f"The submission has moved to marketing review. "
                    f"Marketing team will take further action.\n"
                )
                inviter_email = EmailMessage(
                    subject=inviter_subject,
                    body=inviter_body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[inviter.email],
                )
                inviter_email.send(fail_silently=False)
                _logger.info(
                    "Internal approval notification sent for submission_id=%s to inviter %s",
                    submission.pk, inviter.email,
                )
            except Exception as exc:
                _logger.warning(
                    "Failed to send internal approval notification for submission_id=%s to %s: %s",
                    submission.pk, inviter.email, exc,
                )

        # ── Marketing team notification ───────────────────────────────────
        _notify_marketing_action_required(submission, vendor)

    except Exception as exc:
        _logger.error(
            "Failed to send approval notifications for submission_id=%s: %s",
            submission.pk, exc,
        )


# ---------------------------------------------------------------------------
# 5. Vendor + internal notification on finance rejection
# ---------------------------------------------------------------------------

def notify_vendor_rejected(submission: "VendorOnboardingSubmission", note: str = "") -> None:
    """
    Notify vendor and internal users that finance rejected the submission.

    Args:
        submission: VendorOnboardingSubmission (now in finance_rejected)
        note:       Optional rejection note from finance
    """
    try:
        from django.conf import settings
        from django.core.mail import EmailMessage

        invitation = submission.invitation
        vendor_email = submission.normalized_email or invitation.vendor_email
        vendor_name = submission.normalized_vendor_name or "your registration"
        inviter = getattr(invitation, "invited_by", None)

        # ── Vendor email ──────────────────────────────────────────────────
        subject = "Fund Flow — Vendor Onboarding Requires Attention"
        note_line = f"\nNote from finance team: {note}" if note else ""
        body = (
            f"Dear Vendor,\n\n"
            f"Your vendor registration submission on Fund Flow requires attention.\n\n"
            f"Vendor Name: {vendor_name}\n"
            f"Submission : #{submission.pk}\n"
            f"{note_line}\n\n"
            f"Your registration has not been approved at this time. "
            f"Please review the feedback above and resubmit if appropriate.\n\n"
            f"To resubmit, log in to Fund Flow using your original onboarding link "
            f"and update your registration details.\n\n"
            f"If you believe this is in error, please contact your representative.\n\n"
            f"Regards,\nThe Fund Flow Team"
        )
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[vendor_email],
        )
        email.send(fail_silently=False)
        _logger.info("Vendor rejection email sent for submission_id=%s to %s", submission.pk, vendor_email)

        # ── Internal inviter notification ─────────────────────────────────
        if inviter and inviter.email:
            try:
                inviter_subject = f"[Fund Flow] Vendor Submission Rejected — {vendor_name}"
                inviter_body = (
                    f"A vendor submission has been rejected by finance.\n\n"
                    f"Vendor    : {vendor_name}\n"
                    f"Email     : {vendor_email}\n"
                    f"Submission: #{submission.pk}\n"
                    f"Note      : {note or '(none)'}\n\n"
                    f"The vendor has been notified and can resubmit via the original onboarding link.\n"
                )
                inviter_email = EmailMessage(
                    subject=inviter_subject,
                    body=inviter_body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[inviter.email],
                )
                inviter_email.send(fail_silently=False)
                _logger.info(
                    "Internal rejection notification sent for submission_id=%s to inviter %s",
                    submission.pk, inviter.email,
                )
            except Exception as exc:
                _logger.warning(
                    "Failed to send internal rejection notification for submission_id=%s to %s: %s",
                    submission.pk, inviter.email, exc,
                )

    except Exception as exc:
        _logger.error(
            "Failed to send rejection notifications for submission_id=%s: %s",
            submission.pk, exc,
        )


# ---------------------------------------------------------------------------
# 6. Marketing team notification — vendor awaiting marketing action
# ---------------------------------------------------------------------------

def notify_marketing_action_required(submission: "VendorOnboardingSubmission", vendor: "Vendor") -> None:
    """
    Notify marketing team that a vendor is awaiting marketing review.

    Currently logs for visibility. Wires to marketing team inbox/email
    when that channel is defined.

    Args:
        submission: VendorOnboardingSubmission in marketing_pending
        vendor:     Vendor awaiting marketing approval
    """
    try:
        vendor_name = submission.normalized_vendor_name or "Unknown"
        _logger.info(
            "[VendorOnboarding] Vendor awaiting marketing review. "
            "vendor=%s (id=%s), submission_id=%s, sap_vendor_id=%s",
            vendor_name, vendor.pk, submission.pk, vendor.sap_vendor_id,
        )
        # TODO: wire to marketing notification channel when implemented
    except Exception as exc:
        _logger.warning(
            "Failed to notify marketing for vendor_id=%s: %s",
            vendor.pk, exc,
        )


def _notify_marketing_action_required(submission, vendor):
    """Internal alias for use inside notify_vendor_approved."""
    notify_marketing_action_required(submission, vendor)
