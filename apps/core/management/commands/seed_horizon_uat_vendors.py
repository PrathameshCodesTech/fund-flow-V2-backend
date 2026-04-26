from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.models import Organization, ScopeNode
from apps.invoices.models import VendorInvoiceSubmission
from apps.invoices.services import (
    create_vendor_invoice_submission,
    update_invoice_submission_fields,
)
from apps.users.models import User
from apps.vendors.models import (
    MarketingStatus,
    OperationalStatus,
    UserVendorAssignment,
    Vendor,
)


PASSWORD = "Vendor@123"


VENDOR_SPECS = [
    {
        "name": "Music life",
        "email": "musiclife.vendor@hiparks.com",
        "phone": "+919900000101",
        "portal_email": "musiclife.portal@hiparks.com",
        "sap_vendor_id": "SAP-ML-001",
        "vendor_type": "Creative Agency",
        "title": "Ms",
        "region": "West",
        "gstin": "27ABCDE1234F1Z5",
        "pan": "ABCDE1234F",
        "address_line1": "12 Harmony House",
        "address_line2": "Andheri West",
        "address_line3": "",
        "city": "Mumbai",
        "state": "Maharashtra",
        "country": "India",
        "pincode": "400053",
        "beneficiary_name": "Music life",
        "bank_name": "HDFC Bank",
        "account_number": "50200010010001",
        "bank_account_type": "Current",
        "ifsc": "HDFC0001234",
        "micr_code": "400240123",
        "neft_code": "HDFCNEFT01",
    },
    {
        "name": "ALAAYA",
        "email": "alaaya.vendor@hiparks.com",
        "phone": "+919900000102",
        "portal_email": "alaaya.portal@hiparks.com",
        "sap_vendor_id": "SAP-AL-002",
        "vendor_type": "Events",
        "title": "Mr",
        "region": "North",
        "gstin": "07BCDEA2345G1Z6",
        "pan": "BCDEA2345G",
        "address_line1": "88 Studio Arcade",
        "address_line2": "Sector 18",
        "address_line3": "",
        "city": "Gurugram",
        "state": "Haryana",
        "country": "India",
        "pincode": "122015",
        "beneficiary_name": "ALAAYA",
        "bank_name": "ICICI Bank",
        "account_number": "00120560004567",
        "bank_account_type": "Current",
        "ifsc": "ICIC0004567",
        "micr_code": "110229456",
        "neft_code": "ICICNEFT02",
    },
    {
        "name": "Prisha Designs",
        "email": "prishadesigns.vendor@hiparks.com",
        "phone": "+919900000103",
        "portal_email": "prishadesigns.portal@hiparks.com",
        "sap_vendor_id": "SAP-PD-003",
        "vendor_type": "Printing",
        "title": "Ms",
        "region": "South",
        "gstin": "29CDEAB3456H1Z7",
        "pan": "CDEAB3456H",
        "address_line1": "45 Pixel Point",
        "address_line2": "Indiranagar",
        "address_line3": "",
        "city": "Bengaluru",
        "state": "Karnataka",
        "country": "India",
        "pincode": "560038",
        "beneficiary_name": "Prisha Designs",
        "bank_name": "Axis Bank",
        "account_number": "91002003004005",
        "bank_account_type": "Current",
        "ifsc": "UTIB0003456",
        "micr_code": "560211345",
        "neft_code": "AXISNEFT03",
    },
    {
        "name": "Trupti Graphics",
        "email": "truptigraphics.vendor@hiparks.com",
        "phone": "+919900000104",
        "portal_email": "truptigraphics.portal@hiparks.com",
        "sap_vendor_id": "SAP-TG-004",
        "vendor_type": "Branding",
        "title": "Mr",
        "region": "Incity",
        "gstin": "24DEABC4567J1Z8",
        "pan": "DEABC4567J",
        "address_line1": "7 Creative Mill",
        "address_line2": "Navrangpura",
        "address_line3": "",
        "city": "Ahmedabad",
        "state": "Gujarat",
        "country": "India",
        "pincode": "380009",
        "beneficiary_name": "Trupti Graphics",
        "bank_name": "State Bank of India",
        "account_number": "32000123456789",
        "bank_account_type": "Current",
        "ifsc": "SBIN0007890",
        "micr_code": "380002789",
        "neft_code": "SBINEFT04",
    },
]


class Command(BaseCommand):
    help = "Seed 4 active UAT vendors and 5 READY invoice submissions each, with no send-to route selected."

    @transaction.atomic
    def handle(self, *args, **options):
        org = Organization.objects.get(code="horizon")
        scope_node = ScopeNode.objects.get(org=org, name="Marketing")

        for spec in VENDOR_SPECS:
            vendor, user = self._ensure_vendor_and_portal_user(org, scope_node, spec)
            self._ensure_ready_submissions(vendor, user, scope_node)

        self.stdout.write(self.style.SUCCESS("Seeded 4 UAT vendors with 5 READY invoice submissions each."))
        self.stdout.write(f"Vendor portal default password: {PASSWORD}")

    def _ensure_vendor_and_portal_user(self, org, scope_node, spec):
        user, _ = User.objects.get_or_create(
            email=spec["portal_email"],
            defaults={
                "first_name": spec["name"],
                "last_name": "Portal",
                "is_active": True,
                "is_staff": False,
            },
        )
        user.first_name = spec["name"]
        user.last_name = "Portal"
        user.is_active = True
        user.is_staff = False
        user.set_password(PASSWORD)
        user.save()

        vendor, _ = Vendor.objects.get_or_create(
            org=org,
            vendor_name=spec["name"],
            defaults={
                "scope_node": scope_node,
                "email": spec["email"],
                "phone": spec["phone"],
                "title": spec["title"],
                "vendor_type": spec["vendor_type"],
                "region": spec["region"],
                "gst_registered": True,
                "gstin": spec["gstin"],
                "pan": spec["pan"],
                "address_line1": spec["address_line1"],
                "address_line2": spec["address_line2"],
                "address_line3": spec["address_line3"],
                "city": spec["city"],
                "state": spec["state"],
                "country": spec["country"],
                "pincode": spec["pincode"],
                "preferred_payment_mode": "Bank Transfer",
                "beneficiary_name": spec["beneficiary_name"],
                "bank_name": spec["bank_name"],
                "account_number": spec["account_number"],
                "bank_account_type": spec["bank_account_type"],
                "ifsc": spec["ifsc"],
                "micr_code": spec["micr_code"],
                "neft_code": spec["neft_code"],
                "sap_vendor_id": spec["sap_vendor_id"],
                "po_mandate_enabled": True,
                "marketing_status": MarketingStatus.APPROVED,
                "operational_status": OperationalStatus.ACTIVE,
                "portal_email": spec["portal_email"],
                "portal_user_id": "",
            },
        )

        vendor.scope_node = scope_node
        vendor.email = spec["email"]
        vendor.phone = spec["phone"]
        vendor.title = spec["title"]
        vendor.vendor_type = spec["vendor_type"]
        vendor.region = spec["region"]
        vendor.gst_registered = True
        vendor.gstin = spec["gstin"]
        vendor.pan = spec["pan"]
        vendor.address_line1 = spec["address_line1"]
        vendor.address_line2 = spec["address_line2"]
        vendor.address_line3 = spec["address_line3"]
        vendor.city = spec["city"]
        vendor.state = spec["state"]
        vendor.country = spec["country"]
        vendor.pincode = spec["pincode"]
        vendor.preferred_payment_mode = "Bank Transfer"
        vendor.beneficiary_name = spec["beneficiary_name"]
        vendor.bank_name = spec["bank_name"]
        vendor.account_number = spec["account_number"]
        vendor.bank_account_type = spec["bank_account_type"]
        vendor.ifsc = spec["ifsc"]
        vendor.micr_code = spec["micr_code"]
        vendor.neft_code = spec["neft_code"]
        vendor.sap_vendor_id = spec["sap_vendor_id"]
        vendor.po_mandate_enabled = True
        vendor.marketing_status = MarketingStatus.APPROVED
        vendor.operational_status = OperationalStatus.ACTIVE
        vendor.portal_email = spec["portal_email"]
        vendor.portal_user_id = str(user.pk)
        vendor.save()

        UserVendorAssignment.objects.update_or_create(
            user=user,
            vendor=vendor,
            defaults={"is_active": True},
        )

        return vendor, user

    def _ensure_ready_submissions(self, vendor, user, scope_node):
        existing_refs = set(
            VendorInvoiceSubmission.objects.filter(vendor=vendor)
            .values_list("normalized_data__vendor_invoice_number", flat=True)
        )
        for index in range(1, 6):
            invoice_ref = f"{vendor.vendor_name.upper().replace(' ', '-')}-UAT-{index:03d}"
            if invoice_ref in existing_refs:
                continue

            amount = Decimal("10000.00") + Decimal(index * 2500)
            file_obj = SimpleUploadedFile(
                name=f"{vendor.vendor_name.replace(' ', '_')}_Invoice_{index}.pdf",
                content=b"%PDF-1.4\n% seeded UAT vendor invoice\n",
                content_type="application/pdf",
            )

            submission = create_vendor_invoice_submission(
                user=user,
                vendor=vendor,
                scope_node=scope_node,
                file_obj=file_obj,
            )

            normalized_data = {
                "vendor_invoice_number": invoice_ref,
                "invoice_date": f"2026-04-{10 + index:02d}",
                "due_date": f"2026-05-{10 + index:02d}",
                "currency": "INR",
                "subtotal_amount": str(amount),
                "tax_amount": "0.00",
                "total_amount": str(amount),
                "po_number": f"PO-{vendor.vendor_name.upper().replace(' ', '')}-{index:03d}",
                "description": f"UAT seeded invoice {index} for {vendor.vendor_name}",
            }
            update_invoice_submission_fields(submission, normalized_data)
