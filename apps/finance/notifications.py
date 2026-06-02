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

def _get_logo_url() -> str:
    return getattr(
        settings,
        "EMAIL_BRAND_LOGO_URL",
        f"{getattr(settings, 'FUND_FLOW_BASE_URL', 'http://localhost:3000').rstrip('/')}/hp.jpg",
    )


def _get_base_url() -> str:
    return getattr(settings, "FUND_FLOW_BASE_URL", "http://localhost:3000").rstrip("/")


def _get_invoice_internal_url(invoice_id: int) -> str:
    return f"{_get_base_url()}/invoices/{invoice_id}"


def _send_email_safe(to: list[str], subject: str, body: str, is_html: bool = True) -> bool:
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
        if is_html:
            email.content_subtype = "html"
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
    subject = f"[Horizon] Invoice Approved by Finance — {invoice_title}"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Arial,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:40px 16px;">
    <tr><td align="center">

      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:14px;box-shadow:0 4px 16px rgba(0,0,0,.10);overflow:hidden;">

        <tr>
          <td style="background:linear-gradient(135deg,#ecfdf5 0%,#d1fae5 50%,#ecfdf5 100%);
                     border-bottom:2px solid #6ee7b7;padding:28px 40px;">
            <img src="{_get_logo_url()}" alt="Horizon Industrial Parks" style="height:36px;width:auto;margin-bottom:8px;">
            <h1 style="margin:8px 0 0;font-size:22px;font-weight:700;color:#064e3b;line-height:1.3;">
              ✓ Invoice Approved by Finance
            </h1>
          </td>
        </tr>

        <tr>
          <td style="padding:32px 40px;">
            <p style="margin:0 0 24px;font-size:14px;color:#374151;line-height:1.7;">
              Your invoice has been reviewed and approved by the finance team.
            </p>

            <table cellpadding="0" cellspacing="0" width="100%"
                   style="background:#ecfdf5;border:1px solid #6ee7b7;border-radius:8px;margin-bottom:28px;">
              <tr>
                <td style="padding:16px 20px;">
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#065f46;font-size:13px;white-space:nowrap;">Invoice</td>
                      <td style="padding:4px 0;font-size:13px;font-weight:600;color:#064e3b;">{invoice_title}</td>
                    </tr>
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#065f46;font-size:13px;white-space:nowrap;">Amount</td>
                      <td style="padding:4px 0;font-size:13px;color:#064e3b;">{currency} {invoice_amount}</td>
                    </tr>
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#065f46;font-size:13px;white-space:nowrap;">Reference ID</td>
                      <td style="padding:4px 0;font-size:13px;color:#064e3b;">{reference_id}</td>
                    </tr>
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#065f46;font-size:13px;white-space:nowrap;">Status</td>
                      <td style="padding:4px 0;font-size:13px;font-weight:700;color:#059669;">Approved</td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

            <table cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td align="center">
                  <a href="{internal_url}"
                     style="display:inline-block;padding:14px 32px;background:#ea580c;color:#ffffff;
                            font-size:15px;font-weight:700;text-align:center;text-decoration:none;
                            border-radius:10px;letter-spacing:0.3px;box-shadow:0 2px 8px rgba(234,88,12,.35);">
                    View Invoice Details
                  </a>
                </td>
              </tr>
            </table>

            <p style="margin:24px 0 0;font-size:13px;color:#6b7280;text-align:center;line-height:1.6;">
              This is an automated notification from Horizon.
            </p>
          </td>
        </tr>

        <tr>
          <td style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 40px;">
            <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
              Horizon Industrial Parks &middot; Vendor Invoice Management &middot; Do not reply to this email
            </p>
          </td>
        </tr>

      </table>

    </td></tr>
  </table>

</body>
</html>"""

    return subject, html_body


def _build_rejection_email(
    invoice_title: str,
    invoice_amount: str,
    currency: str,
    rejection_reason: str,
    vendor_email: Optional[str],
    internal_url: str,
) -> tuple[str, str]:
    subject = f"[Horizon] Invoice Rejected by Finance — {invoice_title}"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Arial,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:40px 16px;">
    <tr><td align="center">

      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:14px;box-shadow:0 4px 16px rgba(0,0,0,.10);overflow:hidden;">

        <tr>
          <td style="background:linear-gradient(135deg,#fef2f2 0%,#fee2e2 50%,#fef2f2 100%);
                     border-bottom:2px solid #fecaca;padding:28px 40px;">
            <img src="{_get_logo_url()}" alt="Horizon Industrial Parks" style="height:36px;width:auto;margin-bottom:8px;">
            <h1 style="margin:8px 0 0;font-size:22px;font-weight:700;color:#7f1d1d;line-height:1.3;">
              Invoice Rejected by Finance
            </h1>
          </td>
        </tr>

        <tr>
          <td style="padding:32px 40px;">
            <p style="margin:0 0 24px;font-size:14px;color:#374151;line-height:1.7;">
              Your invoice has been reviewed by the finance team and requires attention.
            </p>

            <table cellpadding="0" cellspacing="0" width="100%"
                   style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;margin-bottom:24px;">
              <tr>
                <td style="padding:16px 20px;">
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#6b7280;font-size:13px;white-space:nowrap;">Invoice</td>
                      <td style="padding:4px 0;font-size:13px;font-weight:600;color:#111827;">{invoice_title}</td>
                    </tr>
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#6b7280;font-size:13px;white-space:nowrap;">Amount</td>
                      <td style="padding:4px 0;font-size:13px;color:#111827;">{currency} {invoice_amount}</td>
                    </tr>
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#6b7280;font-size:13px;white-space:nowrap;">Status</td>
                      <td style="padding:4px 0;font-size:13px;font-weight:700;color:#dc2626;">Rejected</td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

            <table cellpadding="0" cellspacing="0" width="100%"
                   style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;margin-bottom:28px;">
              <tr>
                <td style="padding:16px 20px;">
                  <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#991b1b;text-transform:uppercase;letter-spacing:0.5px;">
                    Rejection Reason
                  </p>
                  <p style="margin:0;font-size:13px;color:#7f1d1d;line-height:1.6;">
                    {rejection_reason}
                  </p>
                </td>
              </tr>
            </table>

            <table cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td align="center">
                  <a href="{internal_url}"
                     style="display:inline-block;padding:14px 32px;background:#ea580c;color:#ffffff;
                            font-size:15px;font-weight:700;text-align:center;text-decoration:none;
                            border-radius:10px;letter-spacing:0.3px;box-shadow:0 2px 8px rgba(234,88,12,.35);">
                    View Invoice Details
                  </a>
                </td>
              </tr>
            </table>

            <p style="margin:24px 0 0;font-size:13px;color:#6b7280;text-align:center;line-height:1.6;">
              This is an automated notification from Horizon.
            </p>
          </td>
        </tr>

        <tr>
          <td style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 40px;">
            <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
              Horizon Industrial Parks &middot; Vendor Invoice Management &middot; Do not reply to this email
            </p>
          </td>
        </tr>

      </table>

    </td></tr>
  </table>

</body>
</html>"""

    return subject, html_body


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

    # Invoice submitter / created_by (internal only; skip vendor portal users)
    submitter = getattr(invoice, "created_by", None)
    if submitter and getattr(submitter, "email", None):
        is_vendor_user = False
        try:
            is_vendor_user = submitter.vendor_assignments.filter(is_active=True).exists()
        except Exception:
            is_vendor_user = False
        email = submitter.email.strip()
        if email and not is_vendor_user and email not in recipients:
            recipients.append(email)

    # Workflow instance started_by / participants (internal only)
    try:
        from apps.workflow.models import WorkflowInstance, WorkflowEvent
        instance = WorkflowInstance.objects.filter(
            subject_type="invoice",
            subject_id=invoice.id,
        ).first()
        if instance:
            if instance.started_by and getattr(instance.started_by, "email", None):
                started_by_is_vendor = False
                try:
                    started_by_is_vendor = instance.started_by.vendor_assignments.filter(is_active=True).exists()
                except Exception:
                    started_by_is_vendor = False
                email = instance.started_by.email.strip()
                if email and not started_by_is_vendor and email not in recipients:
                    recipients.append(email)
            for step in (
                instance.instance_groups.filter(status="in_progress")
                .prefetch_related("instance_steps__assigned_user")
            ):
                for instance_step in step.instance_steps.all():
                    assignee = instance_step.assigned_user
                    if not assignee or not getattr(assignee, "email", None):
                        continue
                    assignee_is_vendor = False
                    try:
                        assignee_is_vendor = assignee.vendor_assignments.filter(is_active=True).exists()
                    except Exception:
                        assignee_is_vendor = False
                    email = assignee.email.strip()
                    if email and not assignee_is_vendor and email not in recipients:
                        recipients.append(email)
            for event in WorkflowEvent.objects.filter(instance=instance).select_related("actor_user"):
                actor = event.actor_user
                if not actor or not getattr(actor, "email", None):
                    continue
                actor_is_vendor = False
                try:
                    actor_is_vendor = actor.vendor_assignments.filter(is_active=True).exists()
                except Exception:
                    actor_is_vendor = False
                email = actor.email.strip()
                if email and not actor_is_vendor and email not in recipients and len(recipients) < 10:
                    recipients.append(email)
    except Exception as exc:
        _logger.warning("Failed to resolve workflow started_by for invoice %s: %s", invoice.id, exc)

    if recipients:
        _send_email_safe(recipients, subject, body)
    else:
        _logger.info("No recipients for invoice %s finance rejection notification.", invoice.id)
