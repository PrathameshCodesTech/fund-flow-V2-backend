"""
Vendor domain services.

All business logic for the vendor onboarding and activation lifecycle lives here.
No view-layer imports.  Django mail is called indirectly via apps.vendors.email
so tests can mock at a single point.
"""
import os
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.vendors.models import (
    FinanceActionType,
    FinanceDecisionChoice,
    InvitationStatus,
    MarketingStatus,
    OperationalStatus,
    SubmissionMode,
    SubmissionStatus,
    UserVendorAssignment,
    Vendor,
    VendorAttachment,
    VendorActivationToken,
    VendorFinanceActionToken,
    VendorFinanceDecision,
    VendorInvitation,
    VendorOnboardingSubmission,
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class InvitationExpiredError(ValueError):
    """Invitation token has passed its expiry."""


class InvitationNotFoundError(ValueError):
    """No invitation found for this token (or it was cancelled)."""


class SubmissionStateError(ValueError):
    """Submission is in the wrong state for the requested operation."""


class VendorStateError(ValueError):
    """Vendor is in the wrong state for the requested operation."""


class FinanceTokenError(ValueError):
    """Finance action token is invalid, expired, or already used."""


class POMandate(ValueError):
    """Vendor requires a PO number but none was supplied."""


# ---------------------------------------------------------------------------
# VRF field mappings (label → normalized field name)
# ---------------------------------------------------------------------------

_VRF_LABEL_MAP = {
    "vendor name": "vendor_name",
    "vendor type": "vendor_type",
    "gst registered": "gst_registered",
    "gstin": "gstin",
    "pan": "pan",
    "email": "email",
    "e-mail": "email",
    "phone": "phone",
    "mobile": "phone",
    "address line 1": "address_line1",
    "address line1": "address_line1",
    "address line 2": "address_line2",
    "address line2": "address_line2",
    "city": "city",
    "state": "state",
    "country": "country",
    "pincode": "pincode",
    "pin code": "pincode",
    "zip code": "pincode",
    "bank name": "bank_name",
    "bank": "bank_name",
    "account number": "account_number",
    "beneficiary account number": "account_number",
    "account no": "account_number",
    "ifsc": "ifsc",
    "ifsc code": "ifsc",
}

_KNOWN_KEYS = set(_VRF_LABEL_MAP.values())

# Keys that map directly from normalized name to model field
_NORM_FIELD_MAP = {
    "vendor_name": "normalized_vendor_name",
    "vendor_type": "normalized_vendor_type",
    "email": "normalized_email",
    "phone": "normalized_phone",
    "gst_registered": "normalized_gst_registered",
    "gstin": "normalized_gstin",
    "pan": "normalized_pan",
    "address_line1": "normalized_address_line1",
    "address_line2": "normalized_address_line2",
    "city": "normalized_city",
    "state": "normalized_state",
    "country": "normalized_country",
    "pincode": "normalized_pincode",
    "bank_name": "normalized_bank_name",
    "account_number": "normalized_account_number",
    "ifsc": "normalized_ifsc",
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _start_finance_review(submission: VendorOnboardingSubmission) -> None:
    """
    Authoritative business transition: move a submission into active finance review.

    Does (in order):
      1. Generate canonical VRF Excel workbook — REQUIRED; raises if fails
      2. Mark submission as SENT_TO_FINANCE with tokens — atomic
      3. Send finance handoff email with workbook — REQUIRED; raises if fails
         (whole transaction rolls back if email cannot be sent with workbook)
      4. Internal audit log notification — non-fatal

    Call within @transaction.atomic.
    """
    from apps.vendors.notifications import (
        send_finance_handoff_notification,
        notify_internal_submission_received,
    )

    # ── 1. Generate canonical VRF Excel (REQUIRED — must succeed before state change) ─
    try:
        generate_vendor_export_excel(submission)
    except Exception as exc:
        raise SubmissionStateError(
            f"Cannot complete finance handoff: VRF workbook generation failed — {exc}"
        ) from exc

    now = timezone.now()

    # ── 2. Authoritative state transition + tokens ────────────────────────────
    submission.status = SubmissionStatus.SENT_TO_FINANCE
    submission.submitted_at = now
    submission.finance_sent_at = now
    submission.save(update_fields=["status", "submitted_at", "finance_sent_at", "updated_at"])

    expiry_hours = getattr(settings, "VENDOR_FINANCE_TOKEN_EXPIRY_HOURS", 72)
    expires_at = now + timedelta(hours=expiry_hours)

    VendorFinanceActionToken.objects.create(
        submission=submission,
        action_type=FinanceActionType.APPROVE,
        token=_generate_token(),
        expires_at=expires_at,
    )
    VendorFinanceActionToken.objects.create(
        submission=submission,
        action_type=FinanceActionType.REJECT,
        token=_generate_token(),
        expires_at=expires_at,
    )

    # ── 3. Finance handoff email (REQUIRED — raises on failure, rolls back transaction) ─
    send_finance_handoff_notification(submission)

    # ── 4. Internal audit log notification (non-fatal) ────────────────────────
    try:
        notify_internal_submission_received(submission)
    except Exception as exc:
        _logger.warning(
            "Internal submission-received notification failed for submission_id=%s: %s",
            submission.pk, exc,
        )


def _generate_token() -> str:
    return secrets.token_urlsafe(48)


def _apply_normalized_fields(submission: VendorOnboardingSubmission, normalized: dict) -> None:
    """Write normalized dict values onto submission model fields."""
    for key, field in _NORM_FIELD_MAP.items():
        if key in normalized:
            value = normalized[key]
            if key == "gst_registered":
                if isinstance(value, bool):
                    setattr(submission, field, value)
                elif isinstance(value, str):
                    setattr(submission, field, value.strip().lower() in ("yes", "true", "1", "y"))
                else:
                    setattr(submission, field, bool(value))
            else:
                setattr(submission, field, str(value).strip() if value is not None else "")


def _extract_normalized_from_payload(payload: dict) -> tuple[dict, dict]:
    """
    Split payload into normalized core fields and remaining raw data.

    Returns (normalized_dict, remaining_raw_dict).
    """
    normalized = {}
    raw = {}
    for key, value in payload.items():
        lower_key = key.strip().lower()
        mapped = _VRF_LABEL_MAP.get(lower_key, lower_key.replace(" ", "_"))
        if mapped in _KNOWN_KEYS:
            normalized[mapped] = value
        else:
            raw[key] = value
    return normalized, raw


def _get_export_dir() -> Path:
    media_root = getattr(settings, "MEDIA_ROOT", settings.BASE_DIR / "media")
    export_dir = Path(media_root) / "vendor_exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir


def _save_source_excel(submission: "VendorOnboardingSubmission", file_bytes: bytes, original_name: str) -> None:
    """Save the original uploaded Excel to disk and record the path on the submission."""
    try:
        source_dir = Path(getattr(settings, "MEDIA_ROOT", settings.BASE_DIR / "media")) / "vendor_source_uploads"
        source_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(original_name).name or "upload.xlsx"
        dest_path = str(source_dir / f"sub_{submission.pk}_source_{safe_name}")
        with open(dest_path, "wb") as fh:
            fh.write(file_bytes)
        submission.source_excel_file = dest_path
        submission.save(update_fields=["source_excel_file", "updated_at"])
    except Exception as exc:
        _logger.warning("Failed to save source Excel for submission_id=%s: %s", submission.pk, exc)


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


# ---------------------------------------------------------------------------
# 1. create_vendor_invitation
# ---------------------------------------------------------------------------

@transaction.atomic
def create_vendor_invitation(
    org,
    scope_node,
    vendor_email: str,
    invited_by=None,
    vendor_name_hint: str = "",
    expires_at=None,
) -> VendorInvitation:
    """
    Create a new vendor invitation with a secure token and send the
    onboarding email to the vendor.

    Returns:
        VendorInvitation with status=pending
    """
    invitation = VendorInvitation.objects.create(
        org=org,
        scope_node=scope_node,
        vendor_email=vendor_email,
        invited_by=invited_by,
        vendor_name_hint=vendor_name_hint,
        token=_generate_token(),
        expires_at=expires_at,
        status=InvitationStatus.PENDING,
    )
    _build_audit_log(
        user=invited_by,
        action="vendor_invitation_created",
        resource_type="VendorInvitation",
        resource_id=invitation.pk,
        metadata={"vendor_email": vendor_email},
    )

    # Send invitation email (mockable in tests)
    _send_invitation_email(invitation, invited_by)

    return invitation


import logging

_logger = logging.getLogger(__name__)


def _send_invitation_email(invitation: VendorInvitation, invited_by) -> None:
    """Send the vendor onboarding invitation email. Mocked in tests."""
    try:
        from apps.vendors.email import send_vendor_invitation_email

        portal_base = getattr(settings, "VENDOR_PORTAL_BASE_URL", "http://localhost:5173")
        onboarding_url = f"{portal_base}/vendor/onboarding/{invitation.token}"
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
    except Exception as exc:
        _logger.warning(
            "Failed to send vendor invitation email for %s (invitation_id=%s): %s",
            invitation.vendor_email,
            invitation.pk,
            exc,
        )


# ---------------------------------------------------------------------------
# 2. get_invitation_by_token
# ---------------------------------------------------------------------------

def get_invitation_by_token(token: str) -> VendorInvitation:
    """
    Look up and validate an invitation by its public token.

    Raises:
        InvitationNotFoundError — if token doesn't exist or invitation is cancelled
        InvitationExpiredError  — if invitation is past its expiry; also marks it expired
    """
    try:
        invitation = VendorInvitation.objects.select_related("org", "scope_node").get(token=token)
    except VendorInvitation.DoesNotExist:
        raise InvitationNotFoundError(f"No active invitation for token.")

    if invitation.status == InvitationStatus.CANCELLED:
        raise InvitationNotFoundError("This invitation has been cancelled.")

    if invitation.is_expired() and invitation.status not in (InvitationStatus.EXPIRED,):
        invitation.status = InvitationStatus.EXPIRED
        invitation.save(update_fields=["status", "updated_at"])

    if invitation.status == InvitationStatus.EXPIRED:
        raise InvitationExpiredError("This invitation has expired.")

    # Mark as opened if still pending
    if invitation.status == InvitationStatus.PENDING:
        invitation.status = InvitationStatus.OPENED
        invitation.save(update_fields=["status", "updated_at"])

    return invitation


# ---------------------------------------------------------------------------
# 3. create_or_update_submission_from_manual
# ---------------------------------------------------------------------------

@transaction.atomic
def create_or_update_submission_from_manual(
    invitation: VendorInvitation,
    payload: dict,
    submitted_by=None,
    finalize: bool = False,
) -> VendorOnboardingSubmission:
    """
    Save or update a manual-entry submission for the given invitation.

    - Stores the full payload in raw_form_data.
    - Extracts normalized core fields.
    - If finalize=True, sets status=submitted and submitted_at=now.

    Raises:
        SubmissionStateError if invitation's active submission is past submitted state.
    """
    # Get existing draft/reopened submission or create new
    existing_qs = invitation.submissions.filter(
        status__in=[SubmissionStatus.DRAFT, SubmissionStatus.REOPENED]
    )
    submission = existing_qs.first()

    if submission is None:
        submission = VendorOnboardingSubmission(invitation=invitation)

    # Validate state
    if submission.pk and submission.status not in (
        SubmissionStatus.DRAFT, SubmissionStatus.REOPENED
    ):
        raise SubmissionStateError(
            f"Submission {submission.pk} is in status '{submission.status}' — cannot edit."
        )

    normalized, remaining_raw = _extract_normalized_from_payload(payload)

    # Preserve any previously stored raw data and merge
    merged_raw = {**submission.raw_form_data, **payload}
    submission.raw_form_data = merged_raw
    submission.submission_mode = SubmissionMode.MANUAL

    _apply_normalized_fields(submission, normalized)

    # Persist payload state before finance transition.
    # For new submissions pk is None — save() inserts; for existing drafts it updates.
    submission.save()

    if finalize:
        # Now that submission is persisted, move it into active finance review.
        # _start_finance_review expects a persisted record with a PK.
        _start_finance_review(submission)
        _build_audit_log(
            user=submitted_by,
            action="vendor_submission_finalized",
            resource_type="VendorOnboardingSubmission",
            resource_id=submission.pk,
            metadata={"mode": "manual"},
        )

    return submission


# ---------------------------------------------------------------------------
# 4. create_or_update_submission_from_excel
# ---------------------------------------------------------------------------

@transaction.atomic
def create_or_update_submission_from_excel(
    invitation: VendorInvitation,
    file_obj,
    submitted_by=None,
    finalize: bool = False,
) -> VendorOnboardingSubmission:
    """
    Parse a VRF-style Excel upload and create/update the submission.

    - Reads each row as label → value pairs.
    - Maps known labels to normalized fields.
    - Unknown labels are preserved in raw_form_data.
    - If finalize=True, sets status=submitted.

    Raises:
        SubmissionStateError if the existing submission is past the editable stage.
    """
    import io as _io
    import openpyxl

    file_obj.seek(0)
    _file_bytes = file_obj.read()
    _file_name = getattr(file_obj, "name", "upload.xlsx")
    wb = openpyxl.load_workbook(_io.BytesIO(_file_bytes), data_only=True)
    ws = wb.active

    extracted: dict = {}
    for row in ws.iter_rows(min_col=1, max_col=2, values_only=True):
        label, value = row[0], row[1]
        if label is None:
            continue
        label_str = str(label).strip()
        if not label_str or value is None:
            continue
        lower = label_str.lower()
        key = _VRF_LABEL_MAP.get(lower, label_str)
        extracted[key] = value

    normalized, _ = _extract_normalized_from_payload(extracted)

    existing_qs = invitation.submissions.filter(
        status__in=[SubmissionStatus.DRAFT, SubmissionStatus.REOPENED]
    )
    submission = existing_qs.first()

    if submission is None:
        submission = VendorOnboardingSubmission(invitation=invitation)

    if submission.pk and submission.status not in (
        SubmissionStatus.DRAFT, SubmissionStatus.REOPENED
    ):
        raise SubmissionStateError(
            f"Submission {submission.pk} is in status '{submission.status}' — cannot edit."
        )

    merged_raw = {**submission.raw_form_data, **extracted}
    submission.raw_form_data = merged_raw
    submission.submission_mode = SubmissionMode.EXCEL_UPLOAD

    _apply_normalized_fields(submission, normalized)

    # Persist payload state before finance transition.
    submission.save()

    # Save the original uploaded file now that we have a pk.
    _save_source_excel(submission, _file_bytes, _file_name)

    if finalize:
        _start_finance_review(submission)
        _build_audit_log(
            user=submitted_by,
            action="vendor_submission_finalized",
            resource_type="VendorOnboardingSubmission",
            resource_id=submission.pk,
            metadata={"mode": "excel_upload"},
        )

    return submission


# ---------------------------------------------------------------------------
# 5. attach_document
# ---------------------------------------------------------------------------

def attach_document(
    submission: VendorOnboardingSubmission,
    title: str,
    file_obj=None,
    file_name: str = "",
    file_url: str = "",
    document_type: str = "",
    uploaded_by=None,
) -> VendorAttachment:
    """
    Create a VendorAttachment record.

    If file_obj is provided (Django uploaded file or file-like), it is stored
    via the FileField on the model (controlled media storage).  file_name is
    derived from file_obj.name if not explicitly supplied.

    For backward compatibility, file_name + file_url without a file_obj still
    work (metadata-only record).
    """
    resolved_name = file_name
    if file_obj and not resolved_name:
        from pathlib import Path
        resolved_name = Path(getattr(file_obj, "name", "attachment")).name

    attachment = VendorAttachment(
        submission=submission,
        title=title,
        file_name=resolved_name,
        file_url=file_url,
        document_type=document_type,
        uploaded_by=uploaded_by,
    )
    if file_obj:
        attachment.file.save(resolved_name, file_obj, save=False)
    attachment.save()
    return attachment


# ---------------------------------------------------------------------------
# 6. generate_vendor_export_excel
# ---------------------------------------------------------------------------

def generate_vendor_export_excel(submission: VendorOnboardingSubmission) -> str:
    """
    Generate the canonical VRF export workbook from the submission's data.

    Writes the file to MEDIA_ROOT/vendor_exports/sub_{id}_vrf.xlsx.
    Updates submission.exported_excel_file and saves the field.

    Returns the filesystem path.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Vendor Registration Form"

    # Styles
    header_font = Font(bold=True, size=13)
    section_font = Font(bold=True, size=11)
    section_fill = PatternFill("solid", fgColor="D9E1F2")
    label_font = Font(bold=True)

    def _section(row_num: int, title: str):
        cell = ws.cell(row=row_num, column=1, value=title)
        cell.font = section_font
        cell.fill = section_fill
        ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=2)
        return row_num + 1

    def _row(row_num: int, label: str, value):
        ws.cell(row=row_num, column=1, value=label).font = label_font
        ws.cell(row=row_num, column=2, value=str(value) if value is not None else "")
        return row_num + 1

    # Title
    title_cell = ws.cell(row=1, column=1, value="Fund Flow - Vendor Registration Form (Export)")
    title_cell.font = header_font
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=2)

    ws.cell(row=2, column=1, value=f"Submission ID: {submission.pk}")
    ws.cell(row=2, column=2, value=f"Status: {submission.status}")

    row = 4

    # Section 1: Company
    row = _section(row, "SECTION 1: COMPANY INFORMATION")
    row = _row(row, "Vendor Name", submission.normalized_vendor_name)
    row = _row(row, "Vendor Type", submission.normalized_vendor_type)
    row = _row(row, "GST Registered", "Yes" if submission.normalized_gst_registered else "No" if submission.normalized_gst_registered is False else "")
    row = _row(row, "GSTIN", submission.normalized_gstin)
    row = _row(row, "PAN", submission.normalized_pan)
    row = _row(row, "Email", submission.normalized_email)
    row = _row(row, "Phone", submission.normalized_phone)
    row += 1

    # Section 2: Address
    row = _section(row, "SECTION 2: ADDRESS")
    row = _row(row, "Address Line 1", submission.normalized_address_line1)
    row = _row(row, "Address Line 2", submission.normalized_address_line2)
    row = _row(row, "City", submission.normalized_city)
    row = _row(row, "State", submission.normalized_state)
    row = _row(row, "Country", submission.normalized_country)
    row = _row(row, "Pincode", submission.normalized_pincode)
    row += 1

    # Section 3: Bank
    row = _section(row, "SECTION 3: BANK DETAILS")
    row = _row(row, "Bank Name", submission.normalized_bank_name)
    row = _row(row, "Account Number", submission.normalized_account_number)
    row = _row(row, "IFSC Code", submission.normalized_ifsc)
    row += 1

    # Section 4: Additional raw data
    raw = submission.raw_form_data or {}
    extra = {k: v for k, v in raw.items() if k not in _NORM_FIELD_MAP and k not in _KNOWN_KEYS}
    if extra:
        row = _section(row, "SECTION 4: ADDITIONAL INFORMATION")
        for k, v in extra.items():
            row = _row(row, k, v)

    # Column widths
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 40

    # Save
    export_dir = _get_export_dir()
    file_path = str(export_dir / f"sub_{submission.pk}_vrf.xlsx")
    wb.save(file_path)

    submission.exported_excel_file = file_path
    submission.save(update_fields=["exported_excel_file", "updated_at"])

    return file_path


# ---------------------------------------------------------------------------
# 7. send_submission_to_finance
# ---------------------------------------------------------------------------

@transaction.atomic
def send_submission_to_finance(
    submission: VendorOnboardingSubmission,
    triggered_by=None,
) -> VendorOnboardingSubmission:
    """
    Generate export workbook, create finance action tokens, send email to
    configured finance recipients, and transition submission to sent_to_finance.

    Submission must be in status=submitted or reopened.

    Raises:
        SubmissionStateError — if submission is not in a sendable state.
    """
    if submission.status not in (SubmissionStatus.SUBMITTED, SubmissionStatus.REOPENED):
        raise SubmissionStateError(
            f"Submission {submission.pk} is in '{submission.status}' — "
            "must be 'submitted' or 'reopened' to send to finance."
        )

    # Generate / refresh export Excel
    export_path = generate_vendor_export_excel(submission)

    # Create (or refresh) finance action tokens
    expiry_hours = getattr(settings, "VENDOR_FINANCE_TOKEN_EXPIRY_HOURS", 72)
    expires_at = timezone.now() + timedelta(hours=expiry_hours)

    approve_token = VendorFinanceActionToken.objects.create(
        submission=submission,
        action_type=FinanceActionType.APPROVE,
        token=_generate_token(),
        expires_at=expires_at,
    )
    reject_token = VendorFinanceActionToken.objects.create(
        submission=submission,
        action_type=FinanceActionType.REJECT,
        token=_generate_token(),
        expires_at=expires_at,
    )

    base_url = getattr(
        settings,
        "VENDOR_FINANCE_PORTAL_BASE_URL",
        getattr(settings, "VENDOR_PORTAL_BASE_URL", "http://localhost:3000"),
    )
    # Both email buttons open the approve-token review page; action query param
    # pre-selects the intended form so the reviewer doesn't need to choose.
    approve_url = f"{base_url}/vendor/finance/{approve_token.token}?action=approve"
    reject_url = f"{base_url}/vendor/finance/{approve_token.token}?action=reject"

    # Gather attachment URLs (legacy file_url only — FileField uploads shown on review page)
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

    # Send email (mockable in tests)
    from apps.vendors.email import send_finance_email
    send_finance_email(
        submission_id=submission.pk,
        vendor_name=submission.normalized_vendor_name or "Unknown Vendor",
        approve_url=approve_url,
        reject_url=reject_url,
        inviting_user=inviting_user,
        scope_name=scope_name,
        exported_excel_path=export_path,
        attachment_urls=attachment_urls,
    )

    submission.status = SubmissionStatus.SENT_TO_FINANCE
    submission.finance_sent_at = timezone.now()
    submission.save(update_fields=["status", "finance_sent_at", "updated_at"])

    _build_audit_log(
        user=triggered_by,
        action="vendor_submission_sent_to_finance",
        resource_type="VendorOnboardingSubmission",
        resource_id=submission.pk,
        metadata={"approve_token": approve_token.pk, "reject_token": reject_token.pk},
    )

    return submission


# ---------------------------------------------------------------------------
# 8. finance_approve_submission
# ---------------------------------------------------------------------------

@transaction.atomic
def finance_approve_submission(
    token_str: str,
    sap_vendor_id: str,
    note: str = "",
) -> tuple[VendorOnboardingSubmission, Vendor]:
    """
    Finance approves the submission via the token link.

    - Token must be action_type=approve, not expired, not used.
    - Submission must be sent_to_finance or reopened.
    - Creates VendorFinanceDecision (approved).
    - Creates or updates the Vendor master record.
    - Transitions submission to marketing_pending.

    Returns (submission, vendor).

    Raises:
        FinanceTokenError      — invalid/expired/used token
        SubmissionStateError   — submission in wrong state
        ValueError             — sap_vendor_id missing
    """
    if not sap_vendor_id or not sap_vendor_id.strip():
        raise ValueError("sap_vendor_id is required for finance approval.")

    token = _get_valid_finance_token(token_str, expected_action=FinanceActionType.APPROVE)
    submission = token.submission

    if submission.status not in (SubmissionStatus.SENT_TO_FINANCE, SubmissionStatus.REOPENED):
        raise SubmissionStateError(
            f"Submission {submission.pk} is in '{submission.status}' — cannot approve."
        )

    now = timezone.now()

    # Mark token used
    token.used_at = now
    token.save(update_fields=["used_at"])

    # Create finance decision
    VendorFinanceDecision.objects.create(
        submission=submission,
        decision=FinanceDecisionChoice.APPROVED,
        sap_vendor_id=sap_vendor_id.strip(),
        note=note,
        acted_via_token=token,
        acted_at=now,
    )

    # Update submission
    submission.finance_vendor_code = sap_vendor_id.strip()
    submission.status = SubmissionStatus.MARKETING_PENDING
    submission.save(update_fields=["finance_vendor_code", "status", "updated_at"])

    # Create or upsert Vendor
    invitation = submission.invitation
    vendor, _ = Vendor.objects.update_or_create(
        onboarding_submission=submission,
        defaults={
            "org": invitation.org,
            "scope_node": invitation.scope_node,
            "vendor_name": submission.normalized_vendor_name or invitation.vendor_name_hint,
            "email": submission.normalized_email,
            "phone": submission.normalized_phone,
            "sap_vendor_id": sap_vendor_id.strip(),
            "marketing_status": MarketingStatus.PENDING,
            "operational_status": OperationalStatus.WAITING_MARKETING_APPROVAL,
        },
    )

    _build_audit_log(
        user=None,
        action="vendor_finance_approved",
        resource_type="VendorOnboardingSubmission",
        resource_id=submission.pk,
        metadata={"sap_vendor_id": sap_vendor_id, "vendor_id": vendor.pk},
    )

    # Notify vendor and inviter of the approval + marketing next step
    from apps.vendors.notifications import notify_vendor_approved
    try:
        notify_vendor_approved(submission, vendor)
    except Exception:
        pass  # Notification failure does not roll back the state transition

    return submission, vendor


# ---------------------------------------------------------------------------
# 9. finance_reject_submission
# ---------------------------------------------------------------------------

@transaction.atomic
def finance_reject_submission(
    token_str: str,
    note: str = "",
) -> VendorOnboardingSubmission:
    """
    Finance rejects the submission via the token link.

    Returns updated submission.

    Raises:
        FinanceTokenError    — invalid/expired/used token
        SubmissionStateError — submission in wrong state
    """
    token = _get_valid_finance_token(token_str, expected_action=FinanceActionType.REJECT)
    submission = token.submission

    if submission.status not in (SubmissionStatus.SENT_TO_FINANCE, SubmissionStatus.REOPENED):
        raise SubmissionStateError(
            f"Submission {submission.pk} is in '{submission.status}' — cannot reject."
        )

    now = timezone.now()
    token.used_at = now
    token.save(update_fields=["used_at"])

    VendorFinanceDecision.objects.create(
        submission=submission,
        decision=FinanceDecisionChoice.REJECTED,
        note=note,
        acted_via_token=token,
        acted_at=now,
    )

    submission.status = SubmissionStatus.FINANCE_REJECTED
    submission.save(update_fields=["status", "updated_at"])

    _build_audit_log(
        user=None,
        action="vendor_finance_rejected",
        resource_type="VendorOnboardingSubmission",
        resource_id=submission.pk,
        metadata={"note": note},
    )

    # Notify vendor and inviter of the rejection
    from apps.vendors.notifications import notify_vendor_rejected
    try:
        notify_vendor_rejected(submission, note=note)
    except Exception:
        pass  # Notification failure does not roll back the state transition

    return submission


# ---------------------------------------------------------------------------
# 10. reopen_submission
# ---------------------------------------------------------------------------

@transaction.atomic
def reopen_submission(
    submission: VendorOnboardingSubmission,
    reopened_by=None,
    note: str = "",
) -> VendorOnboardingSubmission:
    """
    Reopen a finance-rejected submission so the vendor can correct and re-send.

    Only allowed from finance_rejected status.

    Raises:
        SubmissionStateError — if submission is not finance_rejected
    """
    if submission.status != SubmissionStatus.FINANCE_REJECTED:
        raise SubmissionStateError(
            f"Submission {submission.pk} is in '{submission.status}' — "
            "can only reopen from 'finance_rejected'."
        )

    submission.status = SubmissionStatus.REOPENED
    submission.save(update_fields=["status", "updated_at"])

    _build_audit_log(
        user=reopened_by,
        action="vendor_submission_reopened",
        resource_type="VendorOnboardingSubmission",
        resource_id=submission.pk,
        metadata={"note": note},
    )

    return submission


# ---------------------------------------------------------------------------
# 10. Shared portal access helpers
# ---------------------------------------------------------------------------

def get_vendor_email(vendor: Vendor) -> str:
    """Return vendor email or fallback from onboarding submission."""
    if vendor.email:
        return vendor.email
    if vendor.onboarding_submission_id:
        sub = vendor.onboarding_submission
        if sub and getattr(sub, "normalized_email", None):
            return sub.normalized_email
    raise VendorStateError(f"Vendor {vendor.pk} has no email — cannot create portal user.")


def ensure_vendor_portal_user(vendor: Vendor):
    """
    Create or reuse a portal user for vendor.email.
    User is always active (usable after password is set).
    Returns (user, created).
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    vendor_email = get_vendor_email(vendor)
    user, created = User.objects.get_or_create(
        email=vendor_email,
        defaults={"is_active": True},
    )
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    return user, created, vendor_email


def ensure_vendor_portal_assignment(vendor: Vendor, user) -> tuple:
    """Create or reactivate UserVendorAssignment. Returns (assignment, created)."""
    assignment, created = UserVendorAssignment.objects.update_or_create(
        user=user,
        vendor=vendor,
        defaults={"is_active": True},
    )
    return assignment, created


def create_vendor_activation_token(user, vendor, vendor_email: str, actor=None):
    """
    Invalidate all existing un-used tokens for this user,
    then create a fresh activation token.
    Returns (token, created).
    """
    now = timezone.now()
    expiry_days = getattr(settings, "VENDOR_ACTIVATION_TOKEN_EXPIRY_DAYS", 7)
    expires_at = now + timedelta(days=expiry_days)

    # Invalidate any existing un-used tokens for this user
    VendorActivationToken.objects.filter(
        uid=str(user.pk), used_at__isnull=True
    ).update(used_at=now)

    token = VendorActivationToken.objects.create(
        uid=str(user.pk),
        token=secrets.token_urlsafe(48),
        expires_at=expires_at,
        sent_by=actor,
        vendor=vendor,
        sent_at=timezone.now(),
    )
    return token, True


def send_vendor_activation_for_vendor(vendor: Vendor, actor=None):
    """
    Full portal activation flow: ensure user, assignment, token, send email.

    Idempotent:
    - If user/assignment already exist, they are reused.
    - Old unused tokens are invalidated before new one is created.
    - Email send is mandatory — raises on failure.

    Args:
        vendor:    Vendor instance (must be active)
        actor:     User who triggered this (for audit log, can be None)

    Returns dict with result metadata:
        {
            "email": str,
            "user_created": bool,
            "assignment_created": bool,
            "token_created": bool,
        }

    Raises:
        VendorStateError — if vendor is not active or has no email
    """
    if vendor.operational_status != OperationalStatus.ACTIVE:
        raise VendorStateError(
            f"Vendor {vendor.pk} is in '{vendor.operational_status}' — "
            "must be 'active' to send portal activation."
        )

    user, user_created, vendor_email = ensure_vendor_portal_user(vendor)
    assignment, assignment_created = ensure_vendor_portal_assignment(vendor, user)
    token, token_created = create_vendor_activation_token(user, vendor, vendor_email, actor=actor)

    base_url = getattr(settings, "VENDOR_PORTAL_BASE_URL", "http://localhost:3000")
    activation_url = f"{base_url}/vendor/activate/{user.pk}/{token.token}"

    from apps.vendors.email import send_vendor_activation_email
    send_vendor_activation_email(
        vendor_email=vendor_email,
        vendor_name=vendor.vendor_name,
        activation_url=activation_url,
    )

    # Update cached portal tracking fields on Vendor
    vendor.portal_activation_sent_at = timezone.now()
    vendor.portal_user_id = str(user.pk)
    vendor.portal_email = vendor_email
    vendor.save(update_fields=["portal_activation_sent_at", "portal_user_id", "portal_email", "updated_at"])

    _build_audit_log(
        user=actor,
        action="vendor_activation_resent",
        resource_type="Vendor",
        resource_id=vendor.pk,
        metadata={
            "portal_user_id": user.pk,
            "activation_token_id": token.pk,
            "assignment_id": assignment.pk,
            "user_created": user_created,
            "assignment_created": assignment_created,
            "token_created": token_created,
            "email": vendor_email,
        },
    )

    return {
        "email": vendor_email,
        "user_created": user_created,
        "assignment_created": assignment_created,
        "token_created": token_created,
    }


# ---------------------------------------------------------------------------
# 11. approve_vendor_marketing
# ---------------------------------------------------------------------------

@transaction.atomic
def approve_vendor_marketing(
    vendor: Vendor,
    approved_by,
    po_mandate_enabled: bool = False,
) -> Vendor:
    """
    Marketing approves the vendor, making it operational and initiating portal activation.

    - vendor must be operational_status=waiting_marketing_approval.
    - Sets marketing_status=approved, operational_status=active.
    - Sets po_mandate_enabled.
    - Sets linked submission.status=activated.
    - Sends portal activation email (mandatory — rolls back on failure).

    Raises:
        VendorStateError — if vendor is not in waiting_marketing_approval
    """
    if vendor.operational_status != OperationalStatus.WAITING_MARKETING_APPROVAL:
        raise VendorStateError(
            f"Vendor {vendor.pk} is in '{vendor.operational_status}' — "
            "must be 'waiting_marketing_approval' to approve."
        )

    now = timezone.now()
    vendor.marketing_status = MarketingStatus.APPROVED
    vendor.operational_status = OperationalStatus.ACTIVE
    vendor.approved_by_marketing = approved_by
    vendor.approved_at = now
    vendor.po_mandate_enabled = po_mandate_enabled
    vendor.save(update_fields=[
        "marketing_status", "operational_status",
        "approved_by_marketing", "approved_at", "po_mandate_enabled",
        "updated_at",
    ])

    # Transition linked submission
    if vendor.onboarding_submission_id:
        VendorOnboardingSubmission.objects.filter(pk=vendor.onboarding_submission_id).update(
            status=SubmissionStatus.ACTIVATED,
        )

    # Send portal activation (mandatory — raises on failure, rolls back transaction)
    activation_result = send_vendor_activation_for_vendor(vendor, actor=approved_by)

    _build_audit_log(
        user=approved_by,
        action="vendor_marketing_approved",
        resource_type="Vendor",
        resource_id=vendor.pk,
        metadata={
            "po_mandate_enabled": po_mandate_enabled,
            **activation_result,
        },
    )

    return vendor


# ---------------------------------------------------------------------------
# 12. reject_vendor_marketing
# ---------------------------------------------------------------------------

@transaction.atomic
def reject_vendor_marketing(
    vendor: Vendor,
    rejected_by,
    note: str = "",
) -> Vendor:
    """
    Marketing rejects the vendor.

    - Sets marketing_status=rejected, operational_status=inactive.
    - Sets linked submission.status=rejected.

    Raises:
        VendorStateError — if vendor is not in waiting_marketing_approval
    """
    if vendor.operational_status != OperationalStatus.WAITING_MARKETING_APPROVAL:
        raise VendorStateError(
            f"Vendor {vendor.pk} is in '{vendor.operational_status}' — "
            "must be 'waiting_marketing_approval' to reject."
        )

    vendor.marketing_status = MarketingStatus.REJECTED
    vendor.operational_status = OperationalStatus.INACTIVE
    vendor.save(update_fields=["marketing_status", "operational_status", "updated_at"])

    if vendor.onboarding_submission_id:
        VendorOnboardingSubmission.objects.filter(pk=vendor.onboarding_submission_id).update(
            status=SubmissionStatus.REJECTED,
        )

    _build_audit_log(
        user=rejected_by,
        action="vendor_marketing_rejected",
        resource_type="Vendor",
        resource_id=vendor.pk,
        metadata={"note": note},
    )

    return vendor


# ---------------------------------------------------------------------------
# 13. assert_vendor_can_submit_invoice
# ---------------------------------------------------------------------------

def assert_vendor_can_submit_invoice(vendor: Vendor, po_number: str = None) -> None:
    """
    Gate check before a vendor submits an invoice.

    Raises:
        VendorStateError — vendor is not active
        POMandate        — vendor requires PO but none supplied
    """
    if vendor.operational_status != OperationalStatus.ACTIVE:
        raise VendorStateError(
            f"Vendor {vendor.pk} is not active (status: '{vendor.operational_status}'). "
            "Cannot submit invoice."
        )
    if vendor.po_mandate_enabled and not po_number:
        raise POMandate(
            f"Vendor {vendor.pk} requires a PO number for invoice submission."
        )


# ---------------------------------------------------------------------------
# Private: finance token validation
# ---------------------------------------------------------------------------

def _get_valid_finance_token(
    token_str: str,
    expected_action: str,
) -> VendorFinanceActionToken:
    """
    Fetch and validate a finance action token.

    Raises:
        FinanceTokenError — if not found, wrong action, expired, or used
    """
    try:
        token = VendorFinanceActionToken.objects.select_related("submission").get(token=token_str)
    except VendorFinanceActionToken.DoesNotExist:
        raise FinanceTokenError("Invalid finance action token.")

    if token.action_type != expected_action:
        raise FinanceTokenError(
            f"Token is for action '{token.action_type}', expected '{expected_action}'."
        )
    if token.is_used():
        raise FinanceTokenError("This token has already been used.")
    if token.is_expired():
        raise FinanceTokenError("This token has expired.")

    return token


# ---------------------------------------------------------------------------
# Finalize submission (public endpoint helper)
# ---------------------------------------------------------------------------

@transaction.atomic
def finalize_submission(
    submission: VendorOnboardingSubmission,
    submitted_by=None,
) -> VendorOnboardingSubmission:
    """
    Finalize a draft/reopened submission, triggering automatic finance review.

    Under Option B (auto-send-to-finance), this is the canonical finalization
    entry point for draft/reopened submissions. It transitions the submission
    directly to sent_to_finance state.

    Raises:
        SubmissionStateError — if submission is not in draft or reopened
        ValueError           — if minimum required fields are missing
    """
    if submission.status not in (SubmissionStatus.DRAFT, SubmissionStatus.REOPENED):
        raise SubmissionStateError(
            f"Submission {submission.pk} is in '{submission.status}' — cannot finalize."
        )

    if not submission.normalized_vendor_name:
        raise ValueError("vendor_name is required before finalizing.")
    if not submission.normalized_email:
        raise ValueError("email is required before finalizing.")

    # Trigger auto-finance transition (sets status, creates tokens, sends emails)
    _start_finance_review(submission)

    # Mark invitation as submitted
    VendorInvitation.objects.filter(pk=submission.invitation_id).update(
        status=InvitationStatus.SUBMITTED,
    )

    _build_audit_log(
        user=submitted_by,
        action="vendor_submission_finalized",
        resource_type="VendorOnboardingSubmission",
        resource_id=submission.pk,
        metadata={},
    )

    return submission
