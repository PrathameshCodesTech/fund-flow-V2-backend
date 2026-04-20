"""
Invoice Finance Decision Notifications.

Sends email notifications when a finance decision is made on an invoice handoff.
Kept separate from vendor onboarding notifications.

On approval:
  - vendor email (if available)
  - invoice submitter / created_by
  - internal workflow participants (via workflow instance)

On rejection:
  - Same parties + rejection reason in the email body

Notification failures are non-fatal — the decision is recorded regardless of email delivery.
"""

import logging
from typing import Optional

from django.conf import settings
from django.core.mail import EmailMessage

_logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    return getattr(settings, "FUND_FLOW_BASE_URL", "http://localhost:3000").rstrip("/")


def _get_invoice_internal_url(invoice_id: int) -> str:
    return f"{_get_base_url()}/invoices/{invoice_id}"


def _send_email_safe(to: list[str], subject: str, body: str) -> bool:
    """Send email; log failure, never raise."""
    if not to:
        return True
    try:
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to,
        )
        email.send(fail_silently=False)
        _logger.info("Invoice finance notification sent to %s: %s", to, subject)
        return True
    except Exception as exc:
        _logger.warning("Failed to send invoice finance email to %s: %s", to, exc)
        return False


def _build_approval_email(
    invoice_title: str,
    invoice_amount: str,
    currency: str,
    reference_id: str,
    vendor_email: Optional[str],
    internal_url: str,
) -> tuple[str, str]:
    subject = f"[Fund Flow] Invoice Approved by Finance — {invoice_title}"
    body = (
        f"Invoice Finance Review — Approved\n"
        f"================================\n\n"
        f"Invoice  : {invoice_title}\n"
        f"Amount   : {currency} {invoice_amount}\n"
        f"Ref. ID  : {reference_id}\n"
        f"Decision : Finance Approved\n\n"
        f"View invoice: {internal_url}\n\n"
        f"This is an automated notification from Fund Flow.\n"
    )
    return subject, body


def _build_rejection_email(
    invoice_title: str,
    invoice_amount: str,
    currency: str,
    rejection_reason: str,
    vendor_email: Optional[str],
    internal_url: str,
) -> tuple[str, str]:
    subject = f"[Fund Flow] Invoice Rejected by Finance — {invoice_title}"
    body = (
        f"Invoice Finance Review — Rejected\n"
        f"=================================\n\n"
        f"Invoice  : {invoice_title}\n"
        f"Amount   : {currency} {invoice_amount}\n"
        f"Decision : Rejected\n"
        f"Reason   : {rejection_reason}\n\n"
        f"View invoice: {internal_url}\n\n"
        f"This is an automated notification from Fund Flow.\n"
    )
    return subject, body


def notify_invoice_finance_approval(
    invoice,
    reference_id: str,
) -> None:
    """
    Send approval notifications for an invoice finance decision.

    Args:
        invoice: Invoice model instance
        reference_id: Finance's external reference (e.g. SAP ID)
    """
    internal_url = _get_invoice_internal_url(invoice.id)
    vendor_email = getattr(invoice.vendor, "email", None) if invoice.vendor else None
    subject, body = _build_approval_email(
        invoice_title=invoice.title,
        invoice_amount=str(invoice.amount),
        currency=invoice.currency,
        reference_id=reference_id,
        vendor_email=vendor_email,
        internal_url=internal_url,
    )

    # Collect recipients
    recipients: list[str] = []

    # Vendor email
    if vendor_email:
        recipients.append(vendor_email)

    # Invoice submitter / created_by
    submitter = getattr(invoice, "created_by", None)
    if submitter and getattr(submitter, "email", None):
        email = submitter.email.strip()
        if email and email not in recipients:
            recipients.append(email)

    # Workflow instance started_by / participants
    try:
        from apps.workflow.models import WorkflowInstance, WorkflowEvent
        instance = WorkflowInstance.objects.filter(
            subject_type="invoice",
            subject_id=invoice.id,
        ).first()
        if instance:
            if instance.started_by and getattr(instance.started_by, "email", None):
                email = instance.started_by.email.strip()
                if email and email not in recipients:
                    recipients.append(email)
            # Recent workflow participants from timeline
            participant_ids = set()
            for event in WorkflowEvent.objects.filter(instance=instance).select_related("actor_user"):
                actor = event.actor_user
                if actor and getattr(actor, "email", None):
                    email = actor.email.strip()
                    if email and email not in recipients and len(recipients) < 10:
                        recipients.append(email)
    except Exception as exc:
        _logger.warning("Failed to resolve workflow participants for invoice %s: %s", invoice.id, exc)

    if recipients:
        _send_email_safe(recipients, subject, body)
    else:
        _logger.info("No recipients for invoice %s finance approval notification.", invoice.id)


def notify_invoice_finance_rejection(
    invoice,
    rejection_reason: str,
) -> None:
    """
    Send rejection notifications for an invoice finance decision.

    Args:
        invoice: Invoice model instance
        rejection_reason: Reason provided by finance reviewer
    """
    internal_url = _get_invoice_internal_url(invoice.id)
    vendor_email = getattr(invoice.vendor, "email", None) if invoice.vendor else None
    subject, body = _build_rejection_email(
        invoice_title=invoice.title,
        invoice_amount=str(invoice.amount),
        currency=invoice.currency,
        rejection_reason=rejection_reason,
        vendor_email=vendor_email,
        internal_url=internal_url,
    )

    recipients: list[str] = []

    # Vendor email
    if vendor_email:
        recipients.append(vendor_email)

    # Invoice submitter / created_by
    submitter = getattr(invoice, "created_by", None)
    if submitter and getattr(submitter, "email", None):
        email = submitter.email.strip()
        if email and email not in recipients:
            recipients.append(email)

    # Workflow instance started_by
    try:
        from apps.workflow.models import WorkflowInstance
        instance = WorkflowInstance.objects.filter(
            subject_type="invoice",
            subject_id=invoice.id,
        ).first()
        if instance and instance.started_by and getattr(instance.started_by, "email", None):
            email = instance.started_by.email.strip()
            if email and email not in recipients:
                recipients.append(email)
    except Exception as exc:
        _logger.warning("Failed to resolve workflow started_by for invoice %s: %s", invoice.id, exc)

    if recipients:
        _send_email_safe(recipients, subject, body)
    else:
        _logger.info("No recipients for invoice %s finance rejection notification.", invoice.id)
