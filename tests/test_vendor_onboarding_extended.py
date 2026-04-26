"""
Tests for extended vendor onboarding fields (Section F + JSON blocks + MSME).

Covers:
  A. New normalized fields populate on manual submit
  B. JSON blocks store correctly
  C. MSME fields normalize correctly
  D. Unknown extra fields preserved in raw_form_data
  E. Existing old payload still succeeds (backward compat)
  F. Serializer returns new fields
  G. Attachment document types accept new MSME/bank proof values
"""
import io
import pytest

from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.vendors.models import (
    InvitationStatus,
    SubmissionStatus,
    VendorAttachment,
    VendorInvitation,
    VendorOnboardingSubmission,
)
from apps.vendors.services import (
    create_or_update_submission_from_excel,
    create_or_update_submission_from_manual,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Ext Org", code="ext-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/ext-org/hq", depth=0, is_active=True,
    )


@pytest.fixture
def inviter(db):
    return User.objects.create_user(email="inviter@ext.com", password="pass")


@pytest.fixture
def invitation(db, org, company, inviter):
    return VendorInvitation.objects.create(
        org=org,
        scope_node=company,
        invited_by=inviter,
        vendor_email="vendor@ext.com",
        vendor_name_hint="Ext Vendor",
        token="ext-test-token-001",
        status=InvitationStatus.PENDING,
    )


# ---------------------------------------------------------------------------
# A. New normalized fields populate on manual submit
# ---------------------------------------------------------------------------

class TestExtendedNormalizedFields:
    def test_manual_payload_populates_all_new_fields(self, invitation):
        payload = {
            "title": "Mr",
            "vendor_name": "Extended Corp",
            "vendor_type": "Organisation",
            "email": "finance@extcorp.com",
            "phone": "9876543210",
            "fax": "022-12345678",
            "region": "West",
            "head office no": "HO-001",
            "gst_registered": True,
            "gstin": "27AAAAB1234C1Z5",
            "pan": "AAAAA1234A",
            "address line 1": "123 Main Rd",
            "address line 2": "Suite 100",
            "address line 3": "Floor 3",
            "city": "Mumbai",
            "state": "Maharashtra",
            "country": "India",
            "pincode": "400001",
            "preferred payment mode": "NEFT/RTGS",
            "beneficiary name": "Extended Corp",
            "bank name": "State Bank of India",
            "account number": "1234567890",
            "account type": "Current",
            "ifsc": "SBIN0001234",
            "micr code": "123456789",
            "neft code": "NEFT001",
            "bank branch address line 1": "SBIT Mumbai Branch",
            "bank branch address line 2": "Fort",
            "bank branch city": "Mumbai",
            "bank branch state": "Maharashtra",
            "bank branch country": "India",
            "bank branch pincode": "400001",
            "bank phone": "022-99999999",
            "bank fax": "022-88888888",
            "authorized signatory name": "Rajesh Kumar",
            "msme_registered": True,
            "msme_registration_number": "UDYAM-XX-00-0000001",
            "enterprise type": "small",
            "declaration_accepted": True,
        }
        submission = create_or_update_submission_from_manual(
            invitation, payload, submitted_by=invitation.invited_by
        )
        assert submission.normalized_title == "Mr"
        assert submission.normalized_vendor_name == "Extended Corp"
        assert submission.normalized_vendor_type == "Organisation"
        assert submission.normalized_email == "finance@extcorp.com"
        assert submission.normalized_phone == "9876543210"
        assert submission.normalized_fax == "022-12345678"
        assert submission.normalized_region == "West"
        assert submission.normalized_head_office_no == "HO-001"
        assert submission.normalized_gst_registered is True
        assert submission.normalized_gstin == "27AAAAB1234C1Z5"
        assert submission.normalized_pan == "AAAAA1234A"
        assert submission.normalized_address_line1 == "123 Main Rd"
        assert submission.normalized_address_line2 == "Suite 100"
        assert submission.normalized_address_line3 == "Floor 3"
        assert submission.normalized_city == "Mumbai"
        assert submission.normalized_state == "Maharashtra"
        assert submission.normalized_country == "India"
        assert submission.normalized_pincode == "400001"
        assert submission.normalized_preferred_payment_mode == "NEFT/RTGS"
        assert submission.normalized_beneficiary_name == "Extended Corp"
        assert submission.normalized_bank_name == "State Bank of India"
        assert submission.normalized_account_number == "1234567890"
        assert submission.normalized_bank_account_type == "Current"
        assert submission.normalized_ifsc == "SBIN0001234"
        assert submission.normalized_micr_code == "123456789"
        assert submission.normalized_neft_code == "NEFT001"
        assert submission.normalized_bank_branch_address_line1 == "SBIT Mumbai Branch"
        assert submission.normalized_bank_branch_address_line2 == "Fort"
        assert submission.normalized_bank_branch_city == "Mumbai"
        assert submission.normalized_bank_branch_state == "Maharashtra"
        assert submission.normalized_bank_branch_country == "India"
        assert submission.normalized_bank_branch_pincode == "400001"
        assert submission.normalized_bank_phone == "022-99999999"
        assert submission.normalized_bank_fax == "022-88888888"
        assert submission.normalized_authorized_signatory_name == "Rajesh Kumar"
        assert submission.normalized_msme_registered is True
        assert submission.normalized_msme_registration_number == "UDYAM-XX-00-0000001"
        assert submission.normalized_msme_enterprise_type == "small"
        assert submission.declaration_accepted is True

    def test_legacy_fields_still_populated(self, invitation):
        """Existing core fields (pre-extension) must still work as before."""
        payload = {
            "vendor_name": "Legacy Corp",
            "vendor_type": "Company",
            "email": "legacy@corp.com",
            "phone": "1111111111",
            "gst_registered": "yes",
            "gstin": "27LEGACY1234F1Z5",
            "pan": "LEGACY1234F",
            "address line 1": "Old Street",
            "address line 2": "Old Lane",
            "city": "Pune",
            "state": "Maharashtra",
            "country": "India",
            "pincode": "411001",
            "bank name": "Bank of Baroda",
            "account number": "111122223333",
            "ifsc": "BARB0BOMBAN",
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.normalized_vendor_name == "Legacy Corp"
        assert submission.normalized_email == "legacy@corp.com"
        assert submission.normalized_gst_registered is True  # string "yes" → bool
        assert submission.normalized_gstin == "27LEGACY1234F1Z5"
        assert submission.normalized_address_line1 == "Old Street"
        assert submission.normalized_city == "Pune"
        assert submission.normalized_bank_name == "Bank of Baroda"
        assert submission.normalized_ifsc == "BARB0BOMBAN"


# ---------------------------------------------------------------------------
# B. JSON blocks store correctly
# ---------------------------------------------------------------------------

class TestJsonBlocks:
    def test_contact_persons_stored(self, invitation):
        payload = {
            "vendor_name": "JSON Block Corp",
            "email": "test@corp.com",
            "contact_persons": [
                {
                    "type": "general_queries",
                    "name": "Alice",
                    "designation": "Accounts Manager",
                    "email": "alice@corp.com",
                    "telephone": "9999999999",
                },
                {
                    "type": "secondary",
                    "name": "Bob",
                    "designation": "IT Lead",
                    "email": "bob@corp.com",
                    "telephone": "8888888888",
                },
            ],
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.contact_persons_json == payload["contact_persons"]
        assert len(submission.contact_persons_json) == 2
        assert submission.contact_persons_json[0]["type"] == "general_queries"
        assert submission.contact_persons_json[1]["name"] == "Bob"

    def test_head_office_address_stored(self, invitation):
        payload = {
            "vendor_name": "HO Corp",
            "email": "ho@corp.com",
            "head_office_address": {
                "address_line1": "10 HO Street",
                "address_line2": "HO Area",
                "city": "Delhi",
                "state": "Delhi",
                "country": "India",
                "pincode": "110001",
                "phone": "011-22222222",
                "fax": "011-33333333",
            },
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.head_office_address_json["city"] == "Delhi"
        assert submission.head_office_address_json["pincode"] == "110001"
        assert submission.head_office_address_json["phone"] == "011-22222222"

    def test_tax_registration_details_stored(self, invitation):
        payload = {
            "vendor_name": "Tax Corp",
            "email": "tax@corp.com",
            "tax_registration_details": {
                "tax_registration_nos": "ABC123",
                "tin_no": "TIN456",
                "cst_no": "CST789",
                "lst_no": "LST000",
                "esic_reg_no": "ESIC001",
                "pan_ref_no": "PANREF001",
                "ppf_no": "PPF001",
            },
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.tax_registration_details_json["cst_no"] == "CST789"
        assert submission.tax_registration_details_json["esic_reg_no"] == "ESIC001"

    def test_all_json_blocks_together(self, invitation):
        payload = {
            "vendor_name": "All Blocks Corp",
            "email": "all@corp.com",
            "contact_persons": [{"type": "general_queries", "name": "Carol"}],
            "head_office_address": {"city": "Bangalore"},
            "tax_registration_details": {"tin_no": "TIN-BLR-001"},
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.contact_persons_json[0]["name"] == "Carol"
        assert submission.head_office_address_json["city"] == "Bangalore"
        assert submission.tax_registration_details_json["tin_no"] == "TIN-BLR-001"


# ---------------------------------------------------------------------------
# C. MSME fields normalize correctly
# ---------------------------------------------------------------------------

class TestMsmeNormalization:
    @pytest.mark.parametrize("value,expected", [
        (True, True),
        (False, False),
        ("yes", True),
        ("true", True),
        ("1", True),
        ("no", False),
        ("false", False),
        ("0", False),
        ("y", True),
        ("n", False),
    ])
    def test_msme_registered_converts_strings(self, invitation, value, expected):
        payload = {
            "vendor_name": "MSME Test",
            "email": "msme@test.com",
            "msme_registered": value,
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.normalized_msme_registered is expected

    def test_msme_enterprise_type_lowercased(self, invitation):
        payload = {
            "vendor_name": "MSME Type Test",
            "email": "msmetype@test.com",
            "enterprise type": "MEDIUM",
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.normalized_msme_enterprise_type == "medium"

    def test_udyam_registration_no_maps_to_msme_registration_number(self, invitation):
        payload = {
            "vendor_name": "Udyam Test",
            "email": "udyam@test.com",
            "udyam registration no": "UDYAM-XX-11-1111111",
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.normalized_msme_registration_number == "UDYAM-XX-11-1111111"

    def test_declaration_accepted_false(self, invitation):
        payload = {
            "vendor_name": "Decl Test",
            "email": "decl@test.com",
            "declaration_accepted": False,
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.declaration_accepted is False


# ---------------------------------------------------------------------------
# D. Unknown extra fields preserved in raw_form_data
# ---------------------------------------------------------------------------

class TestRawDataPreservation:
    def test_unknown_fields_in_raw_form_data(self, invitation):
        payload = {
            "vendor_name": "Unknown Field Corp",
            "email": "unknown@corp.com",
            "custom_field_xyz": "some value",
            "internal_note": "for procurement team only",
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.raw_form_data.get("custom_field_xyz") == "some value"
        assert submission.raw_form_data.get("internal_note") == "for procurement team only"
        # Core normalized fields also still in raw
        assert submission.raw_form_data.get("vendor_name") == "Unknown Field Corp"

    def test_json_block_keys_also_in_raw(self, invitation):
        """JSON block keys should appear in raw_form_data for audit/completeness."""
        payload = {
            "vendor_name": "Block Corp",
            "email": "block@corp.com",
            "contact_persons": [{"type": "general_queries", "name": "Dan"}],
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        # raw_form_data contains the original payload with block key
        assert submission.raw_form_data.get("vendor_name") == "Block Corp"
        assert submission.raw_form_data.get("contact_persons") is not None


# ---------------------------------------------------------------------------
# E. Backward compat — existing old payload still succeeds
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_minimal_legacy_payload(self, invitation):
        """A payload matching the pre-extension shape must not break."""
        payload = {
            "vendor name": "Min Corp",
            "gst registered": "YES",
            "gstin": "27MINCOR1234F1Z5",
            "pan": "MINCOR1234F",
            "email": "min@corp.com",
            "phone": "1231231234",
            "address line 1": "1 Min St",
            "city": "Chennai",
            "state": "Tamil Nadu",
            "country": "India",
            "pincode": "600001",
            "bank name": "ICICI Bank",
            "account number": "9876543210",
            "ifsc": "ICIC0001234",
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.pk is not None
        assert submission.normalized_vendor_name == "Min Corp"
        assert submission.normalized_gst_registered is True
        assert submission.normalized_gstin == "27MINCOR1234F1Z5"
        assert submission.normalized_bank_name == "ICICI Bank"

    def test_empty_payload_submission(self, invitation):
        """An empty/minimal payload creates a draft submission without error."""
        payload = {}
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.pk is not None
        assert submission.status == SubmissionStatus.DRAFT
        assert submission.normalized_vendor_name == ""


# ---------------------------------------------------------------------------
# F. Serializer returns new fields
# ---------------------------------------------------------------------------

class TestSerializerOutput:
    def test_serializer_includes_new_normalized_fields(self, invitation):
        from apps.vendors.api.serializers import VendorSubmissionSerializer

        payload = {
            "vendor_name": "Serializer Corp",
            "email": "ser@corp.com",
            "title": "Dr",
            "region": "East",
            "preferred payment mode": "RTGS",
            "msme_registered": True,
            "msme_registration_number": "UDYAM-EE-00-9999999",
            "enterprise type": "micro",
            "declaration_accepted": True,
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        serializer = VendorSubmissionSerializer(submission)
        data = serializer.data
        assert data["normalized_title"] == "Dr"
        assert data["normalized_vendor_name"] == "Serializer Corp"
        assert data["normalized_region"] == "East"
        assert data["normalized_preferred_payment_mode"] == "RTGS"
        assert data["normalized_msme_registered"] is True
        assert data["normalized_msme_registration_number"] == "UDYAM-EE-00-9999999"
        assert data["normalized_msme_enterprise_type"] == "micro"
        assert data["declaration_accepted"] is True

    def test_serializer_includes_json_blocks(self, invitation):
        from apps.vendors.api.serializers import VendorSubmissionSerializer

        payload = {
            "vendor_name": "JSON Ser Corp",
            "email": "jsonser@corp.com",
            "contact_persons": [{"type": "general_queries", "name": "Eve"}],
            "head_office_address": {"city": "Hyderabad"},
            "tax_registration_details": {"tin_no": "TIN-HYD-001"},
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        serializer = VendorSubmissionSerializer(submission)
        data = serializer.data
        assert data["contact_persons_json"][0]["name"] == "Eve"
        assert data["head_office_address_json"]["city"] == "Hyderabad"
        assert data["tax_registration_details_json"]["tin_no"] == "TIN-HYD-001"


# ---------------------------------------------------------------------------
# G. Attachment document types accept new values
# ---------------------------------------------------------------------------

class TestAttachmentDocumentTypes:
    def test_attach_document_accepts_msme_types(self, invitation):
        from apps.vendors.services import attach_document

        submission = VendorOnboardingSubmission.objects.create(
            invitation=invitation,
            raw_form_data={"vendor_name": "Att Corp"},
        )
        for doc_type in (
            "msme_declaration_form",
            "msme_registration_certificate",
            "cancelled_cheque",
            "pan_copy",
            "gst_certificate",
            "bank_proof",
            "supporting_document",
        ):
            att = attach_document(
                submission=submission,
                title=f"Test {doc_type}",
                document_type=doc_type,
                uploaded_by=invitation.invited_by,
            )
            assert att.document_type == doc_type


# ---------------------------------------------------------------------------
# H. Hardening: MSME enterprise type validation
# ---------------------------------------------------------------------------

class TestMsmeEnterpriseTypeValidation:
    @pytest.mark.parametrize("value", [
        "micro", "small", "medium",
        "MICRO", "Small", "MEDIUM",
    ])
    def test_valid_enterprise_type_accepted(self, invitation, value):
        payload = {
            "vendor_name": "ET Valid Corp",
            "email": "et@corp.com",
            "enterprise type": value,
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.normalized_msme_enterprise_type == value.lower()

    def test_blank_enterprise_type_allowed(self, invitation):
        payload = {
            "vendor_name": "ET Blank Corp",
            "email": "etblank@corp.com",
            "enterprise type": "",
        }
        submission = create_or_update_submission_from_manual(invitation, payload)
        assert submission.normalized_msme_enterprise_type == ""

    def test_invalid_enterprise_type_raises(self, invitation):
        payload = {
            "vendor_name": "ET Bad Corp",
            "email": "etbad@corp.com",
            "enterprise type": "enterprise_type",
        }
        with pytest.raises(Exception, match="msme_enterprise_type must be one of"):
            create_or_update_submission_from_manual(invitation, payload)

    def test_invalid_enterprise_type_rejected_at_serializer(self, invitation):
        from apps.vendors.api.serializers import ManualSubmissionSerializer
        serializer = ManualSubmissionSerializer(data={
            "data": {
                "vendor_name": "ET Serializer Corp",
                "email": "etser@corp.com",
                "msme_enterprise_type": "not-valid",
            },
            "finalize": False,
        })
        assert serializer.is_valid() is False
        errors_str = str(serializer.errors)
        assert "msme_enterprise_type" in errors_str or "not-valid" in errors_str

    def test_valid_enterprise_type_at_serializer(self, invitation):
        from apps.vendors.api.serializers import ManualSubmissionSerializer
        for val in ("micro", "small", "medium"):
            serializer = ManualSubmissionSerializer(data={
                "data": {
                    "vendor_name": f"ET Ser {val}",
                    "email": f"etser{val}@corp.com",
                    "msme_enterprise_type": val,
                },
                "finalize": False,
            })
            assert serializer.is_valid(), f"{val} should be valid: {serializer.errors}"


# ---------------------------------------------------------------------------
# I. Hardening: Attachment document type validation
# ---------------------------------------------------------------------------

class TestAttachmentDocumentTypeValidation:
    @pytest.mark.parametrize("doc_type", [
        "msme_declaration_form",
        "msme_registration_certificate",
        "cancelled_cheque",
        "pan_copy",
        "gst_certificate",
        "bank_proof",
        "supporting_document",
    ])
    def test_new_doc_type_accepted_by_service(self, invitation, doc_type):
        from apps.vendors.services import attach_document
        submission = VendorOnboardingSubmission.objects.create(
            invitation=invitation,
            raw_form_data={"vendor_name": "Att Type Corp"},
        )
        att = attach_document(
            submission=submission,
            title=f"Test {doc_type}",
            document_type=doc_type,
            uploaded_by=invitation.invited_by,
        )
        assert att.document_type == doc_type

    @pytest.mark.parametrize("doc_type", [
        "msme_declaration_form",
        "msme_registration_certificate",
        "cancelled_cheque",
        "pan_copy",
        "gst_certificate",
        "bank_proof",
        "supporting_document",
    ])
    def test_new_doc_type_accepted_by_serializer(self, invitation, doc_type):
        from apps.vendors.api.serializers import VendorAttachmentCreateSerializer
        serializer = VendorAttachmentCreateSerializer(data={
            "title": f"Test {doc_type}",
            "document_type": doc_type,
        })
        assert serializer.is_valid(), f"{doc_type} should be valid: {serializer.errors}"

    @pytest.mark.parametrize("legacy_type", ["kyc_proof", "address_proof", "gst", "pan", "other"])
    def test_legacy_doc_type_still_accepted(self, invitation, legacy_type):
        from apps.vendors.services import attach_document
        submission = VendorOnboardingSubmission.objects.create(
            invitation=invitation,
            raw_form_data={"vendor_name": "Att Type Corp"},
        )
        att = attach_document(
            submission=submission,
            title=f"Legacy {legacy_type}",
            document_type=legacy_type,
            uploaded_by=invitation.invited_by,
        )
        assert att.document_type == legacy_type

    def test_invalid_doc_type_rejected_by_service(self, invitation):
        from apps.vendors.services import attach_document
        submission = VendorOnboardingSubmission.objects.create(
            invitation=invitation,
            raw_form_data={"vendor_name": "Att Type Corp"},
        )
        with pytest.raises(ValueError, match="document_type 'totally-invalid-type' is not allowed"):
            attach_document(
                submission=submission,
                title="Bad Doc Type",
                document_type="totally-invalid-type",
                uploaded_by=invitation.invited_by,
            )

    def test_invalid_doc_type_rejected_by_serializer(self, invitation):
        from apps.vendors.api.serializers import VendorAttachmentCreateSerializer
        serializer = VendorAttachmentCreateSerializer(data={
            "title": "Bad Doc Type",
            "document_type": "random_unregistered_type",
        })
        assert serializer.is_valid() is False
        assert "document_type" in serializer.errors
