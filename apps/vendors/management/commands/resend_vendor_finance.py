"""
Management command: resend_vendor_finance

Repair/backfill a vendor submission that is in SENT_TO_FINANCE state —
regenerate the Excel workbook and optionally resend the finance email.
Reuses existing valid (non-expired, non-used) APPROVE + REJECT tokens
rather than creating duplicates.

Usage:
    python manage.py resend_vendor_finance --submission-id 9
    python manage.py resend_vendor_finance --submission-id 9 --skip-email
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Regenerate VRF Excel and optionally resend finance email for a submission."

    def add_arguments(self, parser):
        parser.add_argument(
            "--submission-id",
            type=int,
            required=True,
            help="PK of VendorOnboardingSubmission to repair.",
        )
        parser.add_argument(
            "--skip-email",
            action="store_true",
            default=False,
            help="Regenerate Excel only; do not resend the finance email.",
        )

    def handle(self, *args, **options):
        from apps.vendors.models import (
            FinanceActionType,
            SubmissionStatus,
            VendorFinanceActionToken,
            VendorOnboardingSubmission,
        )
        from apps.vendors.notifications import send_finance_handoff_notification
        from apps.vendors.services import generate_vendor_export_excel, _generate_token

        sub_id = options["submission_id"]
        skip_email = options["skip_email"]

        try:
            submission = VendorOnboardingSubmission.objects.select_related(
                "invitation"
            ).get(pk=sub_id)
        except VendorOnboardingSubmission.DoesNotExist:
            raise CommandError(f"No submission with id={sub_id}")

        if submission.status != SubmissionStatus.SENT_TO_FINANCE:
            raise CommandError(
                f"Submission #{sub_id} is in status '{submission.status}', "
                f"expected '{SubmissionStatus.SENT_TO_FINANCE}'. Aborting."
            )

        self.stdout.write(f"Processing submission #{sub_id} ({submission.normalized_vendor_name or 'unknown vendor'}) ...")

        # ── 1. Regenerate Excel ───────────────────────────────────────────────
        self.stdout.write("  Regenerating VRF Excel workbook ...")
        try:
            excel_path = generate_vendor_export_excel(submission)
            self.stdout.write(self.style.SUCCESS(f"  Excel written to: {excel_path}"))
        except Exception as exc:
            raise CommandError(f"Excel generation failed: {exc}") from exc

        # ── 2. Ensure valid APPROVE + REJECT tokens exist ─────────────────────
        with transaction.atomic():
            now = timezone.now()
            from django.conf import settings
            from datetime import timedelta

            expiry_hours = getattr(settings, "VENDOR_FINANCE_TOKEN_EXPIRY_HOURS", 72)
            expires_at = now + timedelta(hours=expiry_hours)

            for action_type in (FinanceActionType.APPROVE, FinanceActionType.REJECT):
                valid_token = (
                    VendorFinanceActionToken.objects.filter(
                        submission=submission,
                        action_type=action_type,
                    )
                    .filter(used_at__isnull=True)
                    .order_by("-created_at")
                    .first()
                )
                if valid_token and not valid_token.is_expired():
                    self.stdout.write(
                        f"  Reusing existing valid {action_type} token (expires {valid_token.expires_at})"
                    )
                else:
                    new_token = VendorFinanceActionToken.objects.create(
                        submission=submission,
                        action_type=action_type,
                        token=_generate_token(),
                        expires_at=expires_at,
                    )
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Created new {action_type} token (expires {new_token.expires_at})"
                        )
                    )

        # ── 3. Resend email (optional) ────────────────────────────────────────
        if skip_email:
            self.stdout.write("  Skipping email resend (--skip-email).")
        else:
            self.stdout.write("  Resending finance handoff email ...")
            try:
                send_finance_handoff_notification(submission)
                self.stdout.write(self.style.SUCCESS("  Email sent."))
            except Exception as exc:
                raise CommandError(f"Email send failed: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"Done. Submission #{sub_id} repaired successfully."))
