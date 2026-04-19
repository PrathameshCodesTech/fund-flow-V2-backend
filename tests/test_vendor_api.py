"""
Vendor API-level tests.

Covers all required endpoint scenarios.
Email sending mocked at apps.vendors.email.send_finance_email.
"""
import io
from unittest.mock import patch

import openpyxl
import pytest
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.test import APIClient

from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.vendors.models import (
    FinanceActionType,
    InvitationStatus,
    MarketingStatus,
    OperationalStatus,
    SubmissionStatus,
    Vendor,
    VendorInvitation,
    VendorOnboardingSubmission,
)
from apps.access.models import Role, UserRoleAssignment
from apps.vendors.services import (
    create_vendor_invitation,
    create_or_update_submission_from_manual,
    finance_approve_submission,
    send_submission_to_finance,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="API Org", code="api-org-v")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq-v",
        node_type=NodeType.COMPANY, path="/api-org-v/hq-v", depth=0, is_active=True,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="ea-v",
        node_type=NodeType.ENTITY, path="/api-org-v/hq-v/ea-v", depth=1, is_active=True,
    )


@pytest.fixture
def user(db, entity):
    u = User.objects.create_user(email="api-vendor-user@example.com", password="pass")
    role, _ = Role.objects.get_or_create(name="Vendor Manager", code="vendor-mgr", org=entity.org, defaults={"name": "Vendor Manager"})
    UserRoleAssignment.objects.create(user=u, role=role, scope_node=entity)
    return u


@pytest.fixture
def client_auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def client_anon():
    return APIClient()


@pytest.fixture
def invitation(org, entity, user):
    return create_vendor_invitation(
        org=org, scope_node=entity,
        vendor_email="api-vendor@example.com",
        invited_by=user,
        vendor_name_hint="API Test Corp",
    )


@pytest.fixture
def manual_payload():
    return {
        "vendor_name": "API Test Corp",
        "vendor_type": "supplier",
        "email": "api-vendor@example.com",
        "phone": "9000000000",
        "address_line1": "100 Test Street",
        "city": "Pune",
        "state": "Maharashtra",
        "country": "India",
        "pincode": "411001",
        "bank_name": "ICICI Bank",
        "account_number": "9876543210",
        "ifsc": "ICIC0001234",
    }


def _submit_and_send(invitation, payload):
    sub = create_or_update_submission_from_manual(invitation, payload, finalize=True)
    with patch("apps.vendors.email.send_finance_email"):
        sub = send_submission_to_finance(sub)
    return sub


def _make_excel_bytes(data: dict) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for label, value in data.items():
        ws.append([label, value])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Invitation create / list / filter
# ---------------------------------------------------------------------------

class TestInvitationEndpoints:
    def test_create_invitation(self, client_auth, org, entity):
        response = client_auth.post("/api/v1/vendors/invitations/", {
            "org": org.id,
            "scope_node": entity.id,
            "vendor_email": "new-vendor@example.com",
            "vendor_name_hint": "New Vendor Co",
        })
        assert response.status_code == http_status.HTTP_201_CREATED
        assert response.data["vendor_email"] == "new-vendor@example.com"
        assert response.data["status"] == InvitationStatus.PENDING

    def test_list_invitations(self, client_auth, invitation):
        response = client_auth.get("/api/v1/vendors/invitations/")
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["count"] >= 1

    def test_filter_by_status(self, client_auth, invitation):
        response = client_auth.get(
            f"/api/v1/vendors/invitations/?status={InvitationStatus.PENDING}"
        )
        assert response.status_code == http_status.HTTP_200_OK
        results = response.data["results"]
        assert all(r["status"] == InvitationStatus.PENDING for r in results)

    def test_filter_by_email(self, client_auth, invitation):
        response = client_auth.get(
            "/api/v1/vendors/invitations/?vendor_email=api-vendor@example.com"
        )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["count"] >= 1


# ---------------------------------------------------------------------------
# 2. Public invitation fetch by token
# ---------------------------------------------------------------------------

class TestPublicInvitationFetch:
    def test_fetch_by_valid_token(self, client_anon, invitation):
        response = client_anon.get(f"/api/v1/vendors/public/invitations/{invitation.token}/")
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["vendor_email"] == invitation.vendor_email

    def test_invalid_token_returns_404(self, client_anon, db):
        response = client_anon.get("/api/v1/vendors/public/invitations/bad_token_xyz/")
        assert response.status_code == http_status.HTTP_404_NOT_FOUND

    def test_cancelled_invitation_returns_404(self, client_anon, invitation):
        invitation.status = InvitationStatus.CANCELLED
        invitation.save()
        response = client_anon.get(f"/api/v1/vendors/public/invitations/{invitation.token}/")
        assert response.status_code == http_status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# 3. Manual submission endpoint
# ---------------------------------------------------------------------------

class TestManualSubmissionEndpoint:
    def test_submit_manual(self, client_anon, invitation, manual_payload):
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/submit-manual/",
            {"data": manual_payload, "finalize": False},
            format="json",
        )
        assert response.status_code in (http_status.HTTP_200_OK, http_status.HTTP_201_CREATED)
        assert response.data["normalized_vendor_name"] == "API Test Corp"
        assert response.data["status"] == SubmissionStatus.DRAFT

    def test_manual_submit_and_finalize(self, client_anon, invitation, manual_payload):
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/submit-manual/",
            {"data": manual_payload, "finalize": True},
            format="json",
        )
        assert response.status_code in (http_status.HTTP_200_OK, http_status.HTTP_201_CREATED)
        assert response.data["status"] == SubmissionStatus.SUBMITTED


# ---------------------------------------------------------------------------
# 4. Excel submission endpoint
# ---------------------------------------------------------------------------

class TestExcelSubmissionEndpoint:
    def test_excel_upload(self, client_anon, invitation):
        data = {
            "Vendor Name": "Excel API Corp",
            "Email": "excel-api@example.com",
            "Phone": "9111111111",
        }
        excel_bytes = _make_excel_bytes(data)
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/submit-excel/",
            {"file": io.BytesIO(excel_bytes)},
            format="multipart",
        )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["normalized_vendor_name"] == "Excel API Corp"

    def test_missing_file_returns_400(self, client_anon, invitation):
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/submit-excel/",
            {},
            format="multipart",
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 5. Attachment endpoint
# ---------------------------------------------------------------------------

class TestAttachmentEndpoint:
    def test_add_attachment(self, client_anon, invitation, manual_payload):
        # First create a submission
        client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/submit-manual/",
            {"data": manual_payload, "finalize": False},
            format="json",
        )
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/attachments/",
            {
                "title": "GST Certificate",
                "file_name": "gst.pdf",
                "file_url": "https://cdn.example.com/gst.pdf",
                "document_type": "gst",
            },
        )
        assert response.status_code == http_status.HTTP_201_CREATED
        assert response.data["title"] == "GST Certificate"

    def test_attachment_without_submission_returns_400(self, client_anon, invitation):
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/attachments/",
            {"title": "Doc", "file_name": "doc.pdf"},
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 6. Finalize endpoint
# ---------------------------------------------------------------------------

class TestFinalizeEndpoint:
    def test_finalize(self, client_anon, invitation, manual_payload):
        # Save draft first
        client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/submit-manual/",
            {"data": manual_payload, "finalize": False},
            format="json",
        )
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/finalize/",
        )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["status"] == SubmissionStatus.SUBMITTED

    def test_finalize_without_submission_returns_400(self, client_anon, invitation):
        response = client_anon.post(
            f"/api/v1/vendors/public/invitations/{invitation.token}/finalize/",
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 7. send-to-finance endpoint
# ---------------------------------------------------------------------------

class TestSendToFinanceEndpoint:
    def test_send_to_finance(self, client_auth, invitation, manual_payload):
        sub = create_or_update_submission_from_manual(invitation, manual_payload, finalize=True)
        with patch("apps.vendors.email.send_finance_email"):
            response = client_auth.post(
                f"/api/v1/vendors/submissions/{sub.id}/send-to-finance/"
            )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["status"] == SubmissionStatus.SENT_TO_FINANCE

    def test_send_to_finance_wrong_state(self, client_auth, invitation, manual_payload):
        sub = create_or_update_submission_from_manual(invitation, manual_payload)
        response = client_auth.post(
            f"/api/v1/vendors/submissions/{sub.id}/send-to-finance/"
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 8. Finance approve endpoint
# ---------------------------------------------------------------------------

class TestFinanceApproveEndpoint:
    def test_finance_approve(self, client_anon, invitation, manual_payload):
        sub = _submit_and_send(invitation, manual_payload)
        approve_token = sub.finance_tokens.get(action_type=FinanceActionType.APPROVE)
        response = client_anon.post(
            f"/api/v1/vendors/public/finance/{approve_token.token}/approve/",
            {"sap_vendor_id": "SAP-API-001", "note": "Looks good"},
            format="json",
        )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["submission"]["status"] == SubmissionStatus.MARKETING_PENDING
        assert response.data["vendor"]["sap_vendor_id"] == "SAP-API-001"

    def test_finance_approve_missing_sap_returns_400(self, client_anon, invitation, manual_payload):
        sub = _submit_and_send(invitation, manual_payload)
        approve_token = sub.finance_tokens.get(action_type=FinanceActionType.APPROVE)
        response = client_anon.post(
            f"/api/v1/vendors/public/finance/{approve_token.token}/approve/",
            {},
            format="json",
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 9. Finance reject endpoint
# ---------------------------------------------------------------------------

class TestFinanceRejectEndpoint:
    def test_finance_reject(self, client_anon, invitation, manual_payload):
        sub = _submit_and_send(invitation, manual_payload)
        reject_token = sub.finance_tokens.get(action_type=FinanceActionType.REJECT)
        response = client_anon.post(
            f"/api/v1/vendors/public/finance/{reject_token.token}/reject/",
            {"note": "Bank details missing"},
            format="json",
        )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["status"] == SubmissionStatus.FINANCE_REJECTED

    def test_used_token_returns_400(self, client_anon, invitation, manual_payload):
        sub = _submit_and_send(invitation, manual_payload)
        reject_token = sub.finance_tokens.get(action_type=FinanceActionType.REJECT)
        # First use
        client_anon.post(
            f"/api/v1/vendors/public/finance/{reject_token.token}/reject/",
            {"note": "First rejection"},
            format="json",
        )
        # Second use should fail
        response = client_anon.post(
            f"/api/v1/vendors/public/finance/{reject_token.token}/reject/",
            {"note": "Second attempt"},
            format="json",
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 10. Vendor list / detail / filter
# ---------------------------------------------------------------------------

class TestVendorListEndpoint:
    def test_list_vendors(self, client_auth, entity, org):
        Vendor.objects.create(
            scope_node=entity, org=org,
            vendor_name="List Vendor", sap_vendor_id="SAP-LIST",
            operational_status=OperationalStatus.ACTIVE,
            marketing_status=MarketingStatus.APPROVED,
        )
        response = client_auth.get("/api/v1/vendors/")
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["count"] >= 1

    def test_filter_by_operational_status(self, client_auth, entity, org):
        Vendor.objects.create(
            scope_node=entity, org=org,
            vendor_name="Active Vendor", sap_vendor_id="SAP-ACT",
            operational_status=OperationalStatus.ACTIVE,
            marketing_status=MarketingStatus.APPROVED,
        )
        response = client_auth.get(
            f"/api/v1/vendors/?operational_status={OperationalStatus.ACTIVE}"
        )
        assert response.status_code == http_status.HTTP_200_OK
        results = response.data["results"]
        assert all(r["operational_status"] == OperationalStatus.ACTIVE for r in results)

    def test_filter_po_mandate(self, client_auth, entity, org):
        Vendor.objects.create(
            scope_node=entity, org=org,
            vendor_name="PO Vendor", sap_vendor_id="SAP-POLIST",
            operational_status=OperationalStatus.ACTIVE,
            marketing_status=MarketingStatus.APPROVED,
            po_mandate_enabled=True,
        )
        response = client_auth.get("/api/v1/vendors/?po_mandate_enabled=true")
        assert response.status_code == http_status.HTTP_200_OK
        results = response.data["results"]
        assert all(r["po_mandate_enabled"] is True for r in results)


# ---------------------------------------------------------------------------
# 11. Marketing approve endpoint
# ---------------------------------------------------------------------------

class TestMarketingApproveEndpoint:
    def test_marketing_approve(self, client_auth, invitation, manual_payload, entity, org):
        sub = _submit_and_send(invitation, manual_payload)
        approve_token = sub.finance_tokens.get(action_type=FinanceActionType.APPROVE)
        _, vendor = finance_approve_submission(approve_token.token, sap_vendor_id="SAP-MKT-E")

        response = client_auth.post(
            f"/api/v1/vendors/{vendor.id}/marketing-approve/",
            {"po_mandate_enabled": True},
            format="json",
        )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["operational_status"] == OperationalStatus.ACTIVE
        assert response.data["po_mandate_enabled"] is True

    def test_marketing_approve_wrong_state_returns_400(self, client_auth, entity, org):
        vendor = Vendor.objects.create(
            scope_node=entity, org=org,
            vendor_name="Wrong State", sap_vendor_id="SAP-WS",
            operational_status=OperationalStatus.INACTIVE,
            marketing_status=MarketingStatus.PENDING,
        )
        response = client_auth.post(
            f"/api/v1/vendors/{vendor.id}/marketing-approve/",
            format="json",
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 12. Marketing reject endpoint
# ---------------------------------------------------------------------------

class TestMarketingRejectEndpoint:
    def test_marketing_reject(self, client_auth, invitation, manual_payload):
        sub = _submit_and_send(invitation, manual_payload)
        approve_token = sub.finance_tokens.get(action_type=FinanceActionType.APPROVE)
        _, vendor = finance_approve_submission(approve_token.token, sap_vendor_id="SAP-MKT-R")

        response = client_auth.post(
            f"/api/v1/vendors/{vendor.id}/marketing-reject/",
            {"note": "Does not meet policy"},
            format="json",
        )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.data["operational_status"] == OperationalStatus.INACTIVE
        assert response.data["marketing_status"] == MarketingStatus.REJECTED

    def test_unauthenticated_rejected(self, client_anon, entity, org):
        vendor = Vendor.objects.create(
            scope_node=entity, org=org,
            vendor_name="Unauth Vendor", sap_vendor_id="SAP-UA",
            operational_status=OperationalStatus.WAITING_MARKETING_APPROVAL,
            marketing_status=MarketingStatus.PENDING,
        )
        response = client_anon.post(f"/api/v1/vendors/{vendor.id}/marketing-reject/")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED
