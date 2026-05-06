"""
Finance email helpers.

Tests mock this at apps.finance.email.send_finance_handoff_email.
"""


def send_finance_handoff_email(
    handoff_id: int,
    module: str,
    subject_name: str,
    approve_url: str,
    reject_url: str,
    export_file_path: str,
    recipients: list[str] = None,
) -> None:
    """
    Send a finance handoff notification email as a styled HTML message.

    Args:
        recipients: list of email addresses to send to.
                    If None, falls back to VENDOR_FINANCE_RECIPIENTS setting
                    for backwards compatibility with non-invoice modules.
    """
    from django.conf import settings
    from django.core.mail import EmailMessage

    if recipients is None:
        recipients = getattr(
            settings,
            "VENDOR_FINANCE_RECIPIENTS",
            getattr(settings, "VENDOR_FINANCE_EMAIL_RECIPIENTS", []),
        )
    if not recipients:
        return

    expiry_hours = getattr(settings, "FINANCE_ACTION_TOKEN_EXPIRY_HOURS", 72)
    subject = f"[VIMS] Finance Review Required — {subject_name}"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Arial,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 16px;">
    <tr><td align="center">

      <!-- Card -->
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);overflow:hidden;">

        <!-- Header bar -->
        <tr>
          <td style="background:linear-gradient(135deg,#fff7ed 0%,#fffbeb 50%,#fff7ed 100%);
                     border-bottom:2px solid #fed7aa;padding:28px 36px;">
            <p style="margin:0;font-size:11px;font-weight:700;letter-spacing:1.5px;color:#c2410c;text-transform:uppercase;">
              VIMS &mdash; Vendor Invoice Management System
            </p>
            <h1 style="margin:6px 0 0;font-size:20px;font-weight:700;color:#7c2d12;line-height:1.3;">
              Finance Review Required
            </h1>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px 36px;">

            <p style="margin:0 0 20px;font-size:14px;color:#374151;line-height:1.6;">
              Dear Finance Team,<br><br>
              A new invoice submission (<strong>{subject_name}</strong>) has been submitted and requires your review.
            </p>

            <p style="margin:0 0 24px;font-size:14px;color:#374151;line-height:1.6;">
              Please click one of the buttons below to review the complete submission details
              and record your decision. Each link opens a secure, one-time review page with all
              supporting documents and vendor information.
            </p>

            <!-- Action buttons -->
            <table cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td align="center" style="padding:0 8px 0 0;" width="50%">
                  <a href="{approve_url}"
                     style="display:block;padding:14px 20px;background:#16a34a;color:#ffffff;
                            font-size:14px;font-weight:700;text-align:center;text-decoration:none;
                            border-radius:8px;letter-spacing:0.3px;">
                    Review Invoice Submission
                  </a>
                </td>
                <td align="center" style="padding:0 0 0 8px;" width="50%">
                  <a href="{reject_url}"
                     style="display:block;padding:14px 20px;background:#dc2626;color:#ffffff;
                            font-size:14px;font-weight:700;text-align:center;text-decoration:none;
                            border-radius:8px;letter-spacing:0.3px;">
                    Reject Invoice Submission
                  </a>
                </td>
              </tr>
            </table>

            <p style="margin:20px 0 0;font-size:12px;color:#9ca3af;text-align:center;">
              These links expire in {expiry_hours} hours and can only be used once.
            </p>

          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 36px;">
            <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
              VIMS &middot; Vendor Invoice Management System &middot; Do not reply to this email
            </p>
          </td>
        </tr>

      </table>
      <!-- /Card -->

    </td></tr>
  </table>

</body>
</html>"""

    email = EmailMessage(
        subject=subject,
        body=html_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    email.content_subtype = "html"
    email.send(fail_silently=False)
