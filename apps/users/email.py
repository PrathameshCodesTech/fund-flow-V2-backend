from __future__ import annotations

from html import escape

from django.conf import settings
from django.core.mail import EmailMessage


def _get_logo_url() -> str:
    return getattr(
        settings,
        "EMAIL_BRAND_LOGO_URL",
        f"{getattr(settings, 'FUND_FLOW_BASE_URL', '').rstrip('/')}/hp.jpg",
    )


def send_internal_password_reset_email(
    *,
    email: str,
    user_name: str,
    reset_url: str,
    requested_by_name: str,
) -> None:
    safe_user_name = escape(user_name or email)
    safe_requested_by = escape(requested_by_name or "an administrator")
    safe_reset_url = escape(reset_url, quote=True)

    subject = "Horizon - Password Reset"
    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:40px 16px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:14px;box-shadow:0 4px 16px rgba(0,0,0,.10);overflow:hidden;">
        <tr>
          <td style="background:#fff7ed;border-bottom:2px solid #fed7aa;padding:28px 40px;">
            <img src="{_get_logo_url()}" alt="Horizon Industrial Parks" style="height:36px;width:auto;margin-bottom:10px;">
            <h1 style="margin:8px 0 0;font-size:22px;font-weight:700;color:#9a3412;line-height:1.3;">
              Password Reset Requested
            </h1>
          </td>
        </tr>
        <tr>
          <td style="padding:32px 40px;">
            <p style="margin:0 0 20px;font-size:14px;color:#374151;line-height:1.7;">
              Dear {safe_user_name},<br><br>
              {safe_requested_by} requested a password reset for your Horizon account.
            </p>
            <p style="margin:0 0 28px;font-size:14px;color:#374151;line-height:1.7;">
              Use the button below to set a new password. If you did not expect this email, you can ignore it.
            </p>
            <p style="margin:0 0 28px;text-align:center;">
              <a href="{safe_reset_url}" style="display:inline-block;background:#f97316;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;padding:12px 22px;border-radius:8px;">
                Set New Password
              </a>
            </p>
            <p style="margin:0;font-size:12px;color:#6b7280;line-height:1.6;">
              For security, this link will expire automatically.
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

    email_msg = EmailMessage(
        subject=subject,
        body=html_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    email_msg.content_subtype = "html"
    email_msg.send(fail_silently=False)
