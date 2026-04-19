"""
Management command: resend_vendor_activation

Resend the vendor portal activation email for an approved (active) vendor.
Uses the shared send_vendor_activation_for_vendor() helper — same logic as
marketing approval and the resend-activation API endpoint.

Usage:
    python manage.py resend_vendor_activation --vendor-id 42
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Regenerate and resend the vendor portal activation email."

    def add_arguments(self, parser):
        parser.add_argument(
            "--vendor-id",
            type=int,
            required=True,
            help="PK of the active Vendor to resend activation for.",
        )

    def handle(self, *args, **options):
        from apps.vendors.models import OperationalStatus, Vendor
        from apps.vendors.services import send_vendor_activation_for_vendor, VendorStateError

        vendor_id = options["vendor_id"]

        try:
            vendor = Vendor.objects.get(pk=vendor_id)
        except Vendor.DoesNotExist:
            raise CommandError(f"No vendor found with id={vendor_id}.")

        if vendor.operational_status != OperationalStatus.ACTIVE:
            raise CommandError(
                f"Vendor #{vendor_id} is in status '{vendor.operational_status}' "
                f"— must be 'active' to resend activation."
            )

        self.stdout.write(
            f"Processing vendor #{vendor_id} ({vendor.vendor_name}) ..."
        )

        try:
            result = send_vendor_activation_for_vendor(vendor, actor=None)
        except VendorStateError as exc:
            raise CommandError(str(exc))
        except Exception as exc:
            raise CommandError(f"Failed to send activation email: {exc}") from exc

        user_created = result["user_created"]
        assignment_created = result["assignment_created"]
        self.stdout.write(
            f"  Email sent to: {result['email']}"
        )
        self.stdout.write(
            f"  User created: {user_created}  |  Assignment created: {assignment_created}  |  Token created: {result['token_created']}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Activation link resent for vendor #{vendor_id} ({vendor.vendor_name})."
            )
        )