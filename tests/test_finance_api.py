"""
Finance API tests.

Covers public token endpoints:
  - GET  /api/v1/finance/public/{token}/       — token metadata
  - POST /api/v1/finance/public/{token}/approve/  — approve via token
  - POST /api/v1/finance/public/{token}/reject/   — reject via token

Internal handoff endpoints:
  - GET  /api/v1/finance/handoffs/           — list (auth required)
  - GET  /api/v1/finance/handoffs/{id}/      — detail (auth required)
  - POST /api/v1/finance/handoffs/{id}/send/  — resend email (auth required)

Email sending mocked at apps.finance.email.send_finance_handoff_email.
"""
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.test import APIClient

from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.access.models import Role, UserRoleAssignment
from apps.invoices.models import Invoice, InvoiceStatus
from apps.campaigns.models import Campaign, CampaignStatus
from apps.finance.models import (
    FinanceActionToken,
    FinanceActionType,
    FinanceHandoff,
    FinanceHandoffStatus,
)
from apps.finance.services import (
    create_finance_handoff,
    finance_approve_handoff,
    finance_reject_handoff,
    send_finance_handoff,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Finance API Org", code="fin-api-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="fin-api-hq",
        node_type=NodeType.COMPANY, path="/fin-api-org/fin-api-hq", depth=0, is_active=True,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="fin-api-ea",
        node_type=NodeType.ENTITY, path="/fin-api-org/fin-api-hq/fin-api-ea",
        depth=1, is_active=True,
    )


@pytest.fixture
def user(db, entity):
    u = User.objects.create_user(email="fin-api-user@example.com", password="pass")
    role, _ = Role.objects.get_or_create(
        name="Finance Viewer", code="fin-viewer", org=entity.org,
        defaults={"name": "Finance Viewer"},
    )
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
def invoice(db, entity, user):
    return Invoice.objects.create(
        scope_node=entity,
        title="API Test Invoice",
        amount="5000.00",
        status=InvoiceStatus.IN_REVIEW,
        created_by=user,
    )


@pytest.fixture
def campaign(db, entity, user):
    return Campaign.objects.create(
        scope_node=entity,
        name="API Test Campaign",
        code="ATC-001",
        requested_amount="10000.00",
        status=CampaignStatus.IN_REVIEW,
        created_by=user,
    )


@pytest.fixture
def mock_email():
    with patch("apps.finance.services._send_finance_email") as mock:
        yield mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_sent_handoff(invoice_or_campaign, entity, user, mock_email):
    """Create and send a finance handoff, returning the handoff and approve token."""
    subject = invoice_or_campaign
    module = "invoice" if isinstance(subject, Invoice) else "campaign"
    h = create_finance_handoff(
        module=module,
        subject_type=module,
        subject_id=subject.pk,
        scope_node=entity,
        submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)
    reject_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.REJECT)
    return h, approve_tok, reject_tok


# ---------------------------------------------------------------------------
# Public: token metadata
# ---------------------------------------------------------------------------

def test_public_token_metadata_approve(client_anon, invoice, entity, user, mock_email):
    h, approve_tok, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_anon.get(f"/api/v1/finance/public/{approve_tok.token}/")
    assert resp.status_code == http_status.HTTP_200_OK
    data = resp.json()
    assert data["action_type"] == "approve"
    assert data["is_expired"] is False
    assert data["is_used"] is False
    assert data["module"] == "invoice"
    assert data["subject_name"] == "API Test Invoice"
    assert data["handoff_status"] == "sent"


def test_public_token_metadata_reject(client_anon, invoice, entity, user, mock_email):
    h, _, reject_tok = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_anon.get(f"/api/v1/finance/public/{reject_tok.token}/")
    assert resp.status_code == http_status.HTTP_200_OK
    data = resp.json()
    assert data["action_type"] == "reject"


def test_public_token_metadata_unknown(db, client_anon):
    resp = client_anon.get("/api/v1/finance/public/unknown-token/")
    assert resp.status_code == http_status.HTTP_404_NOT_FOUND


def test_public_token_metadata_expired(client_anon, invoice, entity, user, mock_email):
    from datetime import timedelta
    h, approve_tok, _ = _build_sent_handoff(invoice, entity, user, mock_email)
    # Backdate expiry
    approve_tok.expires_at = timezone.now() - timedelta(hours=1)
    approve_tok.save(update_fields=["expires_at"])

    resp = client_anon.get(f"/api/v1/finance/public/{approve_tok.token}/")
    assert resp.status_code == http_status.HTTP_200_OK
    assert resp.json()["is_expired"] is True


def test_public_token_metadata_used(client_anon, invoice, entity, user, mock_email):
    h, approve_tok, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    # Use the token first
    with patch("apps.finance.services._build_audit_log"):
        finance_approve_handoff(approve_tok.token, reference_id="REF-001")

    resp = client_anon.get(f"/api/v1/finance/public/{approve_tok.token}/")
    assert resp.status_code == http_status.HTTP_200_OK
    assert resp.json()["is_used"] is True


# ---------------------------------------------------------------------------
# Public: approve endpoint
# ---------------------------------------------------------------------------

def test_public_approve_ok(client_anon, invoice, entity, user, mock_email):
    h, approve_tok, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_anon.post(
        f"/api/v1/finance/public/{approve_tok.token}/approve/",
        data={"reference_id": "SAP-123", "note": "Looks good"},
        format="json",
    )
    assert resp.status_code == http_status.HTTP_200_OK
    data = resp.json()
    assert data["handoff"]["status"] == "finance_approved"
    assert data["handoff"]["finance_reference_id"] == "SAP-123"
    assert data["decision"]["decision"] == "approved"


def test_public_approve_invoice_status_updated(client_anon, invoice, entity, user, mock_email):
    h, approve_tok, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    client_anon.post(
        f"/api/v1/finance/public/{approve_tok.token}/approve/",
        data={"reference_id": "SAP-456"},
        format="json",
    )
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.FINANCE_APPROVED


def test_public_approve_campaign_status_updated(client_anon, campaign, entity, user, mock_email):
    h, approve_tok, _ = _build_sent_handoff(campaign, entity, user, mock_email)

    client_anon.post(
        f"/api/v1/finance/public/{approve_tok.token}/approve/",
        data={"reference_id": "CAMP-789"},
        format="json",
    )
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.FINANCE_APPROVED


def test_public_approve_reject_token_wrong_action(client_anon, invoice, entity, user, mock_email):
    h, _, reject_tok = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_anon.post(
        f"/api/v1/finance/public/{reject_tok.token}/approve/",
        data={"reference_id": "SAP-WRONG"},
        format="json",
    )
    assert resp.status_code == http_status.HTTP_400_BAD_REQUEST
    assert "expected 'approve'" in resp.json()["detail"]


def test_public_approve_missing_reference(client_anon, invoice, entity, user, mock_email):
    h, approve_tok, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_anon.post(
        f"/api/v1/finance/public/{approve_tok.token}/approve/",
        data={},
        format="json",
    )
    assert resp.status_code == http_status.HTTP_400_BAD_REQUEST


def test_public_approve_unknown_token(db, client_anon):
    resp = client_anon.post(
        "/api/v1/finance/public/unknown-token/approve/",
        data={"reference_id": "REF"},
        format="json",
    )
    assert resp.status_code == http_status.HTTP_400_BAD_REQUEST
    assert "Invalid finance action token" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Public: reject endpoint
# ---------------------------------------------------------------------------

def test_public_reject_ok(client_anon, invoice, entity, user, mock_email):
    h, _, reject_tok = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_anon.post(
        f"/api/v1/finance/public/{reject_tok.token}/reject/",
        data={"note": "Missing documents"},
        format="json",
    )
    assert resp.status_code == http_status.HTTP_200_OK
    data = resp.json()
    assert data["handoff"]["status"] == "finance_rejected"
    assert data["decision"]["decision"] == "rejected"
    assert data["decision"]["note"] == "Missing documents"


def test_public_reject_invoice_status_updated(client_anon, invoice, entity, user, mock_email):
    h, _, reject_tok = _build_sent_handoff(invoice, entity, user, mock_email)

    client_anon.post(
        f"/api/v1/finance/public/{reject_tok.token}/reject/",
        data={"note": "Bad invoice"},
        format="json",
    )
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.FINANCE_REJECTED


def test_public_reject_campaign_status_updated(client_anon, campaign, entity, user, mock_email):
    h, _, reject_tok = _build_sent_handoff(campaign, entity, user, mock_email)

    client_anon.post(
        f"/api/v1/finance/public/{reject_tok.token}/reject/",
        data={"note": "Over budget"},
        format="json",
    )
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.FINANCE_REJECTED


def test_public_reject_approve_token_wrong_action(client_anon, invoice, entity, user, mock_email):
    h, approve_tok, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_anon.post(
        f"/api/v1/finance/public/{approve_tok.token}/reject/",
        data={"note": "Rejected"},
        format="json",
    )
    assert resp.status_code == http_status.HTTP_400_BAD_REQUEST
    assert "expected 'reject'" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Internal: authenticated endpoints
# ---------------------------------------------------------------------------

def test_handoff_list_requires_auth(client_anon):
    resp = client_anon.get("/api/v1/finance/handoffs/")
    # JWT auth returns 401 when no credentials are provided
    assert resp.status_code == http_status.HTTP_401_UNAUTHORIZED


def test_handoff_list_auth_ok(client_auth, invoice, entity, user, mock_email):
    h, _, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_auth.get("/api/v1/finance/handoffs/")
    assert resp.status_code == http_status.HTTP_200_OK
    data = resp.json()
    assert data["count"] >= 1


def test_handoff_detail_auth_ok(client_auth, invoice, entity, user, mock_email):
    h, _, _ = _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_auth.get(f"/api/v1/finance/handoffs/{h.pk}/")
    assert resp.status_code == http_status.HTTP_200_OK
    assert resp.json()["id"] == h.pk


def test_handoff_detail_unknown(client_auth):
    resp = client_auth.get("/api/v1/finance/handoffs/99999/")
    assert resp.status_code == http_status.HTTP_404_NOT_FOUND


def test_handoff_send_action(client_auth, invoice, entity, user, mock_email):
    h, _, _ = _build_sent_handoff(invoice, entity, user, mock_email)
    # Handoff is already SENT — sending again should fail
    resp = client_auth.post(f"/api/v1/finance/handoffs/{h.pk}/send/")
    assert resp.status_code == http_status.HTTP_400_BAD_REQUEST


def test_handoff_list_filter_by_module(client_auth, invoice, entity, user, mock_email):
    _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_auth.get("/api/v1/finance/handoffs/?module=campaign")
    assert resp.status_code == http_status.HTTP_200_OK
    # Invoice handoff should not appear in campaign-filtered results
    results = resp.json()["results"] if "results" in resp.json() else resp.json()
    for item in results:
        assert item["module"] == "campaign"


def test_handoff_list_filter_by_status(client_auth, invoice, entity, user, mock_email):
    _build_sent_handoff(invoice, entity, user, mock_email)

    resp = client_auth.get("/api/v1/finance/handoffs/?status=sent")
    assert resp.status_code == http_status.HTTP_200_OK
    results = resp.json()["results"] if "results" in resp.json() else resp.json()
    for item in results:
        assert item["status"] == "sent"
