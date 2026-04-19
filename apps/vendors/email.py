"""
Vendor email helpers.

All public functions accept plain data — no model imports at module level to
avoid circular dependencies.  Tests mock `apps.vendors.email.*`.
"""
from django.conf import settings
from django.core.mail import EmailMessage


def send_vendor_invitation_email(
    vendor_email: str,
    vendor_name_hint: str,
    onboarding_url: str,
    invited_by_name: str,
) -> None:
    subject = "You've been invited to Fund Flow — Vendor Onboarding"

    if vendor_name_hint:
        greeting = f"Hello {vendor_name_hint},"
    else:
        greeting = "Hello,"

    body = (
        f"{greeting}\n\n"
        f"You have been invited to complete your vendor onboarding via Fund Flow.\n\n"
        f"To get started, click the link below:\n\n"
        f"  {onboarding_url}\n\n"
        f"This onboarding link is personal and should not be shared.\n"
        f"Please complete your registration at your earliest convenience.\n\n"
        f"If you have questions, please contact your {invited_by_name} representative.\n\n"
        f"Regards,\n"
        f"The Fund Flow Team"
    )

    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[vendor_email],
    )
    email.send(fail_silently=False)


def send_vendor_activation_email(
    vendor_email: str,
    vendor_name: str,
    activation_url: str,
) -> None:
    """
    Send the vendor portal activation email with a styled "Set Password" button.

    Args:
        vendor_email:     Target vendor email address
        vendor_name:      Normalized vendor name (shown in email body)
        activation_url:   Full URL: /vendor/activate/{uid}/{token}
    """
    expiry_days = getattr(settings, "VENDOR_ACTIVATION_TOKEN_EXPIRY_DAYS", 7)
    subject = f"Fund Flow — Activate Your Vendor Portal Account"

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Arial,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:40px 16px;">
    <tr><td align="center">

      <!-- Card -->
      <table width="560" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:14px;box-shadow:0 4px 16px rgba(0,0,0,.10);overflow:hidden;">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1e3a8a 0%,#2563eb 100%);padding:32px 40px;">
            <p style="margin:0;font-size:10px;font-weight:600;letter-spacing:2px;color:#bfdbfe;text-transform:uppercase;">
              Fund Flow
            </p>
            <h1 style="margin:8px 0 0;font-size:22px;font-weight:700;color:#ffffff;line-height:1.3;">
              Activate Your Vendor Portal
            </h1>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px 40px;">

            <p style="margin:0 0 24px;font-size:14px;color:#374151;line-height:1.7;">
              Dear {vendor_name or 'Vendor'},<br><br>
              Your vendor account has been approved and is now ready for activation.
              Please set your password to access the vendor self-service portal.
            </p>

            <!-- Vendor badge -->
            <table cellpadding="0" cellspacing="0" width="100%"
                   style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;margin-bottom:28px;">
              <tr>
                <td style="padding:14px 18px;">
                  <p style="margin:0;font-size:12px;color:#0369a1;font-weight:600;">Vendor Account</p>
                  <p style="margin:4px 0 0;font-size:15px;font-weight:700;color:#0c4a6e;">{vendor_name or 'Fund Flow Vendor'}</p>
                </td>
              </tr>
            </table>

            <!-- CTA Button -->
            <table cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td align="center">
                  <a href="{activation_url}"
                     style="display:inline-block;padding:15px 40px;background:#16a34a;color:#ffffff;
                            font-size:16px;font-weight:700;text-align:center;text-decoration:none;
                            border-radius:10px;letter-spacing:0.3px;box-shadow:0 2px 8px rgba(22,163,74,.35);">
                    🔐&nbsp;&nbsp;Set Password & Activate
                  </a>
                </td>
              </tr>
            </table>

            <p style="margin:20px 0 0;font-size:13px;color:#6b7280;text-align:center;line-height:1.6;">
              This activation link expires in <strong>{expiry_days} days</strong>.<br>
              If you did not expect this email, please ignore or contact your administrator.
            </p>

          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:16px 40px;">
            <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
              Fund Flow · Vendor Self-Service Portal · Do not reply to this email
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
        to=[vendor_email],
    )
    email.content_subtype = "html"
    email.send(fail_silently=False)


def send_finance_email(
    submission_id: int,
    vendor_name: str,
    approve_url: str,
    reject_url: str,
    inviting_user: str | None = None,
    scope_name: str | None = None,
    exported_excel_path: str | None = None,
    attachment_urls: list[str] | None = None,
    recipient_list: list[str] | None = None,
) -> None:
    """
    Send the VRF package to finance recipients as an HTML email with
    inline-CSS Approve / Reject action buttons.

    Args:
        submission_id:       ID of VendorOnboardingSubmission (for subject line)
        vendor_name:         Normalized vendor name
        approve_url:         Full URL to the approve-token finance review page
        reject_url:          Full URL to the reject-token finance review page
        inviting_user:       Display name of the internal user who invited the vendor
        scope_name:          Name of the scope node (entity/company) for context
        exported_excel_path: Filesystem path to the generated VRF Excel (attached)
        attachment_urls:     Vendor-uploaded attachment file URLs (mentioned in body)
        recipient_list:      Explicit list of recipient emails.
    """
    recipients = recipient_list if recipient_list else (
        getattr(settings, "VENDOR_FINANCE_RECIPIENTS", None) or ["finance@example.com"]
    )
    if isinstance(recipients, str):
        recipients = [recipients]

    expiry_hours = getattr(settings, "VENDOR_FINANCE_TOKEN_EXPIRY_HOURS", 72)
    subject = f"[Fund Flow] Vendor Onboarding Review Required: {vendor_name} (Submission #{submission_id})"

    # ── Context rows ──────────────────────────────────────────────────────────
    context_rows = ""
    if inviting_user:
        context_rows += f"""
            <tr>
              <td style="padding:4px 12px 4px 0;color:#6b7280;font-size:13px;white-space:nowrap;">Invited by</td>
              <td style="padding:4px 0;font-size:13px;color:#111827;">{inviting_user}</td>
            </tr>"""
    if scope_name:
        context_rows += f"""
            <tr>
              <td style="padding:4px 12px 4px 0;color:#6b7280;font-size:13px;white-space:nowrap;">Entity</td>
              <td style="padding:4px 0;font-size:13px;color:#111827;">{scope_name}</td>
            </tr>"""

    # ── Attachment list ───────────────────────────────────────────────────────
    att_section = ""
    if attachment_urls:
        items = "".join(
            f'<li style="margin:4px 0;"><a href="{url}" style="color:#2563eb;">{url}</a></li>'
            for url in attachment_urls
        )
        att_section = f"""
        <p style="margin:24px 0 6px;font-size:13px;color:#374151;font-weight:600;">
          Vendor Attachments
        </p>
        <ul style="margin:0;padding-left:18px;font-size:13px;color:#374151;">
          {items}
        </ul>"""

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
          <td style="background:linear-gradient(135deg,#1e40af 0%,#3b82f6 100%);padding:28px 36px;">
            <p style="margin:0;font-size:11px;font-weight:600;letter-spacing:1.5px;color:#bfdbfe;text-transform:uppercase;">
              Fund Flow
            </p>
            <h1 style="margin:6px 0 0;font-size:20px;font-weight:700;color:#ffffff;line-height:1.3;">
              Vendor Onboarding Review
            </h1>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:32px 36px;">

            <p style="margin:0 0 20px;font-size:14px;color:#374151;line-height:1.6;">
              Dear Finance Team,<br><br>
              A vendor onboarding submission is ready for your review.
              Please examine the details below and take action.
            </p>

            <!-- Vendor info card -->
            <table cellpadding="0" cellspacing="0" width="100%"
                   style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;margin-bottom:24px;">
              <tr>
                <td style="padding:16px 20px;">
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#6b7280;font-size:13px;white-space:nowrap;">Vendor Name</td>
                      <td style="padding:4px 0;font-size:13px;font-weight:600;color:#111827;">{vendor_name}</td>
                    </tr>
                    <tr>
                      <td style="padding:4px 12px 4px 0;color:#6b7280;font-size:13px;white-space:nowrap;">Submission #</td>
                      <td style="padding:4px 0;font-size:13px;color:#111827;">#{submission_id}</td>
                    </tr>
                    {context_rows}
                  </table>
                </td>
              </tr>
            </table>

            {att_section}

            <!-- Divider -->
            <hr style="border:none;border-top:1px solid #e5e7eb;margin:28px 0;">

            <p style="margin:0 0 20px;font-size:14px;color:#374151;line-height:1.6;">
              Review the attached VRF workbook and supporting documents, then use the
              buttons below to record your decision. Each button opens a secure,
              one-time review page — <strong>no login required</strong>.
            </p>

            <!-- Action buttons -->
            <table cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td align="center" style="padding:0 8px 0 0;" width="50%">
                  <a href="{approve_url}"
                     style="display:block;padding:14px 0;background:#16a34a;color:#ffffff;
                            font-size:15px;font-weight:700;text-align:center;text-decoration:none;
                            border-radius:8px;letter-spacing:0.3px;">
                    &#10003;&nbsp;&nbsp;Approve Vendor
                  </a>
                </td>
                <td align="center" style="padding:0 0 0 8px;" width="50%">
                  <a href="{reject_url}"
                     style="display:block;padding:14px 0;background:#dc2626;color:#ffffff;
                            font-size:15px;font-weight:700;text-align:center;text-decoration:none;
                            border-radius:8px;letter-spacing:0.3px;">
                    &#10007;&nbsp;&nbsp;Reject Submission
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
              This email was sent by Fund Flow · Do not reply to this email
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

    if exported_excel_path:
        try:
            with open(exported_excel_path, "rb") as fh:
                email.attach(
                    f"VRF_{vendor_name.replace(' ', '_')}_#{submission_id}.xlsx",
                    fh.read(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
        except OSError:
            raise  # Workbook must be attached — do not send without it

    email.send(fail_silently=False)