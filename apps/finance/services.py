"""
Finance domain services.

Generic finance handoff layer for modules (invoice, campaign, vendor)
that require an internal workflow approval followed by an external finance review.

Architecture:
    - FinanceHandoff: the record of sending a subject to external finance
    - FinanceActionToken: approve/reject tokens emailed to finance
    - FinanceDecision: the recorded outcome of a token action

Subject sync:
    When a handoff is approved/rejected, the subject's status is updated
    via sync_subject_on_finance_change(). This is called at the end of
    every handoff state transition.
"""
import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.finance.models import (
    FinanceActionToken,
    FinanceActionType,
    FinanceDecision,
    FinanceDecisionChoice,
    FinanceHandoff,
    FinanceHandoffStatus,
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class FinanceHandoffError(ValueError):
    """Base exception for finance handoff errors."""


class HandoffNotFoundError(FinanceHandoffError):
    """No handoff found for the given token."""


class HandoffStateError(FinanceHandoffError):
    """Handoff is not in a valid state for the requested operation."""


class TokenError(FinanceHandoffError):
    """Token is invalid, expired, or already used."""


class NoFinanceRecipientsError(FinanceHandoffError):
    """No eligible finance recipients could be resolved for the handoff."""


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------

def resolve_finance_recipients_for_handoff(handoff: FinanceHandoff) -> list[str]:
    """
    Resolve finance recipient emails for a handoff.

    For invoice handoffs: dynamically resolves users with finance_team role
    at the invoice's scope_node or its ancestor chain.

    For other modules: falls back to VENDOR_FINANCE_RECIPIENTS setting.

    Raises:
        NoFinanceRecipientsError â€” for invoice handoffs when no finance users exist
    """
    if handoff.module == "invoice":
        return _resolve_invoice_finance_recipients(handoff)
    return _get_static_finance_recipients()


def _resolve_invoice_finance_recipients(handoff: FinanceHandoff) -> list[str]:
    """
    Resolve finance team users for an invoice handoff using role assignments.

    Walks up the scope node chain to find users with finance roles.
    Raises NoFinanceRecipientsError if none are found.
    """
    from apps.invoices.models import Invoice
    try:
        invoice = Invoice.objects.select_related("scope_node__org").get(pk=handoff.subject_id)
    except Invoice.DoesNotExist:
        raise NoFinanceRecipientsError(
            f"Cannot resolve recipients: Invoice {handoff.subject_id} not found."
        )

    scope_node = invoice.scope_node
    org = getattr(scope_node, "org", None)

    if not org:
        raise NoFinanceRecipientsError(
            f"Cannot resolve recipients: Invoice scope node has no organisation."
        )

    # Walk up scope chain: entity -> company -> org root
    scope_ids = [scope_node.id]
    if hasattr(scope_node, "path") and scope_node.path:
        from apps.core.services import get_ancestors
        for ancestor in get_ancestors(scope_node):
            if ancestor.id not in scope_ids:
                scope_ids.append(ancestor.id)

    # Get finance role codes
    role_codes = _get_finance_role_codes()

    from django.contrib.auth import get_user_model
    User = get_user_model()
    users = User.objects.filter(
        role_assignments__role__org=org,
        role_assignments__role__code__in=role_codes,
        role_assignments__scope_node_id__in=scope_ids,
        is_active=True,
    ).exclude(email="").exclude(email__isnull=True).distinct()

    emails = sorted(set(u.email.strip().lower() for u in users if u.email))
    if not emails:
        raise NoFinanceRecipientsError(
            f"No eligible finance recipients found for invoice scope '{scope_node.name}' "
            f"(id={scope_node.id}). Assign finance_team role to users at this scope "
            f"or its parent entity."
        )
    return emails


def _get_static_finance_recipients() -> list[str]:
    """Return static finance recipients from settings (for non-invoice modules)."""
    from django.conf import settings
    fallback = getattr(
        settings,
        "VENDOR_FINANCE_RECIPIENTS",
        getattr(settings, "VENDOR_FINANCE_EMAIL_RECIPIENTS", []),
    )
    if isinstance(fallback, str):
        fallback = [fallback]
    return list(fallback) if fallback else []


def _get_finance_role_codes() -> set[str]:
    """Return the set of role codes that qualify as finance recipients."""
    from django.conf import settings
    return set(getattr(settings, "FINANCE_ROLE_CODES", {"finance_team"}))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _generate_token() -> str:
    return secrets.token_urlsafe(48)


def _get_export_dir() -> Path:
    media_root = getattr(settings, "MEDIA_ROOT", settings.BASE_DIR / "media")
    export_dir = Path(media_root) / "finance_exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir


def _build_audit_log(user, action: str, resource_type: str, resource_id: int, metadata: dict = None) -> None:
    """Write to AuditLog if the audit app is available."""
    try:
        from apps.audit.models import AuditLog
        AuditLog.objects.create(
            user=user,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata or {},
        )
    except Exception:
        pass  # Audit failure must never block business logic


def _send_finance_email(
    handoff_id: int,
    module: str,
    subject_name: str,
    approve_url: str,
    reject_url: str,
    export_file_path: str,
    recipients: list[str],
) -> None:
    """
    Send finance handoff email to the resolved finance recipients.
    Mockable in tests at apps.finance.email.send_finance_handoff_email.
    """
    from apps.finance.email import send_finance_handoff_email

    try:
        send_finance_handoff_email(
            handoff_id=handoff_id,
            module=module,
            subject_name=subject_name,
            approve_url=approve_url,
            reject_url=reject_url,
            export_file_path=export_file_path,
            recipients=recipients,
        )
    except Exception as exc:
        raise FinanceHandoffError(f"Failed to send finance handoff email: {exc}") from exc


# ---------------------------------------------------------------------------
# Subject sync  (called after every handoff state change)
# ---------------------------------------------------------------------------

def sync_subject_on_finance_change(handoff: FinanceHandoff) -> None:
    """
    Update the subject domain object's status when a handoff state changes.

    Supported modules:
        invoice  â†’ Invoice.status
        campaign â†’ Campaign.status

    Transitions:
        finance_approved  â†’ finance_approved
        finance_rejected  â†’ finance_rejected
    """
    if handoff.module == "invoice":
        from apps.invoices.models import Invoice, InvoiceStatus
        status_map = {
            FinanceHandoffStatus.FINANCE_APPROVED: InvoiceStatus.FINANCE_APPROVED,
            FinanceHandoffStatus.FINANCE_REJECTED: InvoiceStatus.FINANCE_REJECTED,
        }
        new_status = status_map.get(handoff.status)
        if new_status:
            Invoice.objects.filter(pk=handoff.subject_id).update(
                status=new_status,
                updated_at=timezone.now(),
            )

    elif handoff.module == "campaign":
        from apps.campaigns.models import Campaign, CampaignStatus
        status_map = {
            FinanceHandoffStatus.FINANCE_APPROVED: CampaignStatus.FINANCE_APPROVED,
            FinanceHandoffStatus.FINANCE_REJECTED: CampaignStatus.FINANCE_REJECTED,
        }
        new_status = status_map.get(handoff.status)
        if new_status:
            Campaign.objects.filter(pk=handoff.subject_id).update(
                status=new_status,
                updated_at=timezone.now(),
            )


# ---------------------------------------------------------------------------
# Subject name helper (used in email)
# ---------------------------------------------------------------------------

def _get_subject_name(handoff: FinanceHandoff) -> str:
    """Return a human-readable name for the handoff subject."""
    if handoff.module == "invoice":
        from apps.invoices.models import Invoice
        try:
            return Invoice.objects.get(pk=handoff.subject_id).title
        except Invoice.DoesNotExist:
            return f"Invoice {handoff.subject_id}"
    elif handoff.module == "campaign":
        from apps.campaigns.models import Campaign
        try:
            return Campaign.objects.get(pk=handoff.subject_id).name
        except Campaign.DoesNotExist:
            return f"Campaign {handoff.subject_id}"
    return f"{handoff.subject_type} {handoff.subject_id}"


# ---------------------------------------------------------------------------
# 1. Create handoff
# ---------------------------------------------------------------------------

@transaction.atomic
def create_finance_handoff(
    module: str,
    subject_type: str,
    subject_id: int,
    scope_node,
    org=None,
    submitted_by=None,
    export_data: dict = None,
) -> FinanceHandoff:
    """
    Create a new FinanceHandoff in PENDING status.

    Raises:
        HandoffStateError â€” if an active (pending/sent) handoff already exists
                           for this subject.

    Args:
        module:       'invoice' | 'campaign' | ...
        subject_type: 'invoice' | 'campaign' | ...
        subject_id:   PK of the subject record
        scope_node:   ScopeNode instance
        org:          Organization (fetched from scope_node if not given)
        submitted_by: User who triggered this (for audit)
        export_data:  Optional dict of data to include in the export file
    """
    if org is None:
        org = scope_node.org

    # Ensure no active handoff already exists for this subject
    active = FinanceHandoff.objects.filter(
        module=module,
        subject_type=subject_type,
        subject_id=subject_id,
        status__in=[
            FinanceHandoffStatus.PENDING,
            FinanceHandoffStatus.SENT,
        ],
    ).exists()
    if active:
        raise HandoffStateError(
            f"An active finance handoff already exists for {module}:{subject_type}:{subject_id}."
        )

    # Build export file if data provided
    export_file = ""
    if export_data:
        export_file = _build_export_file(handoff_id=None, module=module, data=export_data)

    handoff = FinanceHandoff.objects.create(
        org=org,
        scope_node=scope_node,
        module=module,
        subject_type=subject_type,
        subject_id=subject_id,
        status=FinanceHandoffStatus.PENDING,
        export_file=export_file,
        submitted_by=submitted_by,
    )

    # If we had export_data, write the file now that we have the handoff id
    if export_data and not export_file:
        export_file = _build_export_file(handoff_id=handoff.id, module=module, data=export_data)
        handoff.export_file = export_file
        handoff.save(update_fields=["export_file"])

    _build_audit_log(
        user=submitted_by,
        action="finance_handoff_created",
        resource_type="FinanceHandoff",
        resource_id=handoff.pk,
        metadata={
            "module": module,
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
    )

    return handoff


# ---------------------------------------------------------------------------
# 2. Build export file
# ---------------------------------------------------------------------------

def _build_export_file(handoff_id: Optional[int], module: str, data: dict) -> str:
    """
    Generate a JSON export file for the handoff.
    Returns the filesystem path.
    """
    import json

    export_dir = _get_export_dir()
    suffix = f"handoff_{handoff_id or 'new'}.json" if handoff_id else "handoff_new.json"
    file_path = export_dir / f"{module}_{suffix}"

    payload = {
        "module": module,
        "generated_at": timezone.now().isoformat(),
        "handoff_id": handoff_id,
        "data": data,
    }

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    return str(file_path)


# ---------------------------------------------------------------------------
# 3. Send handoff to finance
# ---------------------------------------------------------------------------

@transaction.atomic
def send_finance_handoff(
    handoff: FinanceHandoff,
    triggered_by=None,
) -> FinanceHandoff:
    """
    Create approve + reject tokens, email finance recipients, and mark handoff SENT.

    Args:
        handoff: FinanceHandoff instance in PENDING or SENT status
        triggered_by: User who triggered the send (for audit)

    Returns:
        Updated FinanceHandoff (status=SENT, sent_at set)

    Raises:
        HandoffStateError â€” if handoff is not in PENDING status
        NoFinanceRecipientsError â€” if no eligible finance recipients can be resolved
    """
    if handoff.status not in (FinanceHandoffStatus.PENDING, FinanceHandoffStatus.SENT):
        raise HandoffStateError(
            f"Handoff {handoff.pk} is in status '{handoff.status}', expected 'pending' or 'sent'."
        )

    expiry_hours = getattr(settings, "VENDOR_FINANCE_TOKEN_EXPIRY_HOURS", 72)
    expires_at = timezone.now() + timedelta(hours=expiry_hours)

    # Resend semantics: invalidate previous unused links so the latest email
    # remains the single actionable entry point.
    FinanceActionToken.objects.filter(
        handoff=handoff,
        used_at__isnull=True,
    ).update(used_at=timezone.now())

    # Create approve and reject tokens
    approve_token = FinanceActionToken.objects.create(
        handoff=handoff,
        action_type=FinanceActionType.APPROVE,
        token=_generate_token(),
        expires_at=expires_at,
    )
    reject_token = FinanceActionToken.objects.create(
        handoff=handoff,
        action_type=FinanceActionType.REJECT,
        token=_generate_token(),
        expires_at=expires_at,
    )

    review_base_url = getattr(
        settings,
        "FINANCE_REVIEW_BASE_URL",
        getattr(settings, "VENDOR_PORTAL_BASE_URL", "http://localhost:3000"),
    ).rstrip("/")
    approve_url = f"{review_base_url}/finance/review/{approve_token.token}"
    reject_url = f"{review_base_url}/finance/review/{reject_token.token}"

    subject_name = _get_subject_name(handoff)

    # Resolve recipients dynamically (raises NoFinanceRecipientsError for invoices if none found)
    recipients = resolve_finance_recipients_for_handoff(handoff)

    # Send email (mockable) â€” only mark SENT after email succeeds
    _send_finance_email(
        handoff_id=handoff.pk,
        module=handoff.module,
        subject_name=subject_name,
        approve_url=approve_url,
        reject_url=reject_url,
        export_file_path=handoff.export_file,
        recipients=recipients,
    )

    # Mark handoff sent only after email succeeds
    handoff.status = FinanceHandoffStatus.SENT
    handoff.sent_at = timezone.now()
    handoff.save(update_fields=["status", "sent_at", "updated_at"])

    _build_audit_log(
        user=triggered_by,
        action="finance_handoff_sent",
        resource_type="FinanceHandoff",
        resource_id=handoff.pk,
        metadata={
            "approve_token_id": approve_token.pk,
            "reject_token_id": reject_token.pk,
        },
    )

    return handoff


# ---------------------------------------------------------------------------
# 4. Approve via token
# ---------------------------------------------------------------------------

@transaction.atomic
def finance_approve_handoff(
    token_str: str,
    reference_id: str,
    note: str = "",
) -> tuple[FinanceHandoff, FinanceDecision]:
    """
    Record a finance approval via the token link.

    Args:
        token_str:    The action token from the email link
        reference_id: Finance's external reference (e.g. SAP vendor code, PO number)
        note:         Optional approval note

    Returns:
        (handoff, decision) tuple

    Raises:
        TokenError       â€” invalid, expired, or already-used token, or wrong action type
        HandoffStateError â€” handoff is not in a sendable state
        ValueError       â€” reference_id is empty
    """
    if not reference_id or not reference_id.strip():
        raise ValueError("reference_id is required for finance approval.")

    token = _get_valid_finance_token(token_str, expected_action=FinanceActionType.APPROVE)
    handoff = token.handoff

    if handoff.status not in (FinanceHandoffStatus.PENDING, FinanceHandoffStatus.SENT):
        raise HandoffStateError(
            f"Handoff {handoff.pk} is in '{handoff.status}' â€” cannot approve."
        )

    now = timezone.now()

    # Mark token used
    token.used_at = now
    token.save(update_fields=["used_at"])

    # Record decision
    decision = FinanceDecision.objects.create(
        handoff=handoff,
        decision=FinanceDecisionChoice.APPROVED,
        reference_id=reference_id.strip(),
        note=note,
        acted_via_token=token,
        acted_at=now,
    )

    # Update handoff
    handoff.status = FinanceHandoffStatus.FINANCE_APPROVED
    handoff.finance_reference_id = reference_id.strip()
    handoff.save(update_fields=["status", "finance_reference_id", "updated_at"])

    # Sync subject status
    sync_subject_on_finance_change(handoff)

    _build_audit_log(
        user=None,
        action="finance_handoff_approved",
        resource_type="FinanceHandoff",
        resource_id=handoff.pk,
        metadata={
            "reference_id": reference_id,
            "decision_id": decision.pk,
        },
    )

    return handoff, decision


# ---------------------------------------------------------------------------
# 5. Reject via token
# ---------------------------------------------------------------------------

@transaction.atomic
def finance_reject_handoff(
    token_str: str,
    note: str = "",
) -> tuple[FinanceHandoff, FinanceDecision]:
    """
    Record a finance rejection via the token link.

    Args:
        token_str: The action token from the email link
        note:      Rejection reason

    Returns:
        (handoff, decision) tuple

    Raises:
        TokenError       â€” invalid, expired, or already-used token, or wrong action type
        HandoffStateError â€” handoff is not in a sendable state
    """
    token = _get_valid_finance_token(token_str, expected_action=FinanceActionType.REJECT)
    handoff = token.handoff

    if handoff.status not in (FinanceHandoffStatus.PENDING, FinanceHandoffStatus.SENT):
        raise HandoffStateError(
            f"Handoff {handoff.pk} is in '{handoff.status}' â€” cannot reject."
        )

    now = timezone.now()

    # Mark token used
    token.used_at = now
    token.save(update_fields=["used_at"])

    # Record decision
    decision = FinanceDecision.objects.create(
        handoff=handoff,
        decision=FinanceDecisionChoice.REJECTED,
        note=note,
        acted_via_token=token,
        acted_at=now,
    )

    # Update handoff
    handoff.status = FinanceHandoffStatus.FINANCE_REJECTED
    handoff.save(update_fields=["status", "updated_at"])

    # Sync subject status
    sync_subject_on_finance_change(handoff)

    _build_audit_log(
        user=None,
        action="finance_handoff_rejected",
        resource_type="FinanceHandoff",
        resource_id=handoff.pk,
        metadata={"decision_id": decision.pk, "note": note},
    )

    return handoff, decision


# ---------------------------------------------------------------------------
# 6. Get active handoff for subject
# ---------------------------------------------------------------------------

def get_active_handoff_for_subject(
    module: str,
    subject_type: str,
    subject_id: int,
) -> FinanceHandoff | None:
    """
    Return the currently active (pending or sent) handoff for a subject,
    or None if none exists.
    """
    return FinanceHandoff.objects.filter(
        module=module,
        subject_type=subject_type,
        subject_id=subject_id,
        status__in=[
            FinanceHandoffStatus.PENDING,
            FinanceHandoffStatus.SENT,
        ],
    ).first()


# ---------------------------------------------------------------------------
# 7. Get handoff by token (for public API metadata)
# ---------------------------------------------------------------------------

def get_handoff_by_token(token_str: str) -> FinanceHandoff:
    """
    Look up a handoff by its action token.

    Raises:
        TokenError â€” token not found
    """
    try:
        return FinanceActionToken.objects.select_related("handoff").get(token=token_str).handoff
    except FinanceActionToken.DoesNotExist:
        raise TokenError("Invalid finance action token.")


# ---------------------------------------------------------------------------
# Private: token validation
# ---------------------------------------------------------------------------

def _get_valid_finance_token(
    token_str: str,
    expected_action: str,
) -> FinanceActionToken:
    """
    Fetch and validate a finance action token.

    Raises:
        TokenError â€” not found, wrong action, expired, or already used
    """
    try:
        token = FinanceActionToken.objects.select_related("handoff").get(token=token_str)
    except FinanceActionToken.DoesNotExist:
        raise TokenError("Invalid finance action token.")

    if token.action_type != expected_action:
        raise TokenError(
            f"Token is for action '{token.action_type}', expected '{expected_action}'."
        )
    if token.is_used():
        raise TokenError("This token has already been used.")
    if token.is_expired():
        raise TokenError("This token has expired.")

    return token

