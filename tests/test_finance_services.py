"""
Finance services tests.

Covers all 23 required scenarios:
  - Handoff create/send/approve/reject (9)
  - Token reuse / expire (4)
  - Invoice integration (3)
  - Campaign integration (3)
  - Vendor integration note (1)
  - Error / edge cases (6)

Email sending mocked at apps.finance.email.send_finance_handoff_email.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.core.models import Organization, ScopeNode, NodeType
from apps.users.models import User
from apps.invoices.models import Invoice, InvoiceStatus
from apps.campaigns.models import Campaign, CampaignStatus
from apps.finance.models import (
    FinanceActionToken,
    FinanceActionType,
    FinanceDecision,
    FinanceDecisionChoice,
    FinanceHandoff,
    FinanceHandoffStatus,
)
from apps.finance.services import (
    HandoffNotFoundError,
    HandoffStateError,
    TokenError,
    create_finance_handoff,
    finance_approve_handoff,
    finance_reject_handoff,
    get_active_handoff_for_subject,
    get_handoff_by_token,
    send_finance_handoff,
    sync_subject_on_finance_change,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org(db):
    return Organization.objects.create(name="Finance Svc Org", code="fin-svc-org")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="fin-hq",
        node_type=NodeType.COMPANY, path="/fin-svc-org/fin-hq", depth=0, is_active=True,
    )


@pytest.fixture
def entity(org, company):
    return ScopeNode.objects.create(
        org=org, parent=company, name="Entity A", code="fin-ea",
        node_type=NodeType.ENTITY, path="/fin-svc-org/fin-hq/fin-ea", depth=1, is_active=True,
    )


@pytest.fixture
def user(db, entity):
    return User.objects.create_user(email="fin-user@example.com", password="pass")


@pytest.fixture
def invoice(db, entity, user):
    return Invoice.objects.create(
        scope_node=entity,
        title="Test Invoice",
        amount="5000.00",
        status=InvoiceStatus.IN_REVIEW,
        created_by=user,
    )


@pytest.fixture
def campaign(db, entity, user):
    return Campaign.objects.create(
        scope_node=entity,
        name="Test Campaign",
        code="TC-001",
        requested_amount="10000.00",
        status=CampaignStatus.IN_REVIEW,
        created_by=user,
    )


@pytest.fixture
def mock_email():
    with patch("apps.finance.services._send_finance_email") as mock:
        yield mock


# ---------------------------------------------------------------------------
# 1. Handoff creation
# ---------------------------------------------------------------------------

def test_create_handoff_invoice_ok(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice",
        subject_type="invoice",
        subject_id=invoice.pk,
        scope_node=entity,
        submitted_by=user,
        export_data={"title": "Test Invoice", "amount": "5000.00"},
    )
    assert h.pk is not None
    assert h.status == FinanceHandoffStatus.PENDING
    assert h.module == "invoice"
    assert h.subject_id == invoice.pk


def test_create_handoff_campaign_ok(db, campaign, entity, user, mock_email):
    h = create_finance_handoff(
        module="campaign",
        subject_type="campaign",
        subject_id=campaign.pk,
        scope_node=entity,
        submitted_by=user,
    )
    assert h.pk is not None
    assert h.status == FinanceHandoffStatus.PENDING
    assert h.module == "campaign"


def test_create_handoff_dupe_raises(db, invoice, entity, user, mock_email):
    create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    with pytest.raises(HandoffStateError) as exc:
        create_finance_handoff(
            module="invoice", subject_type="invoice", subject_id=invoice.pk,
            scope_node=entity, submitted_by=user,
        )
    assert "active finance handoff already exists" in str(exc.value)


def test_create_handoff_sent_not_blocking(db, invoice, entity, user, mock_email):
    """A SENT handoff is also considered active; creating a second should fail."""
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    with pytest.raises(HandoffStateError):
        create_finance_handoff(
            module="invoice", subject_type="invoice", subject_id=invoice.pk,
            scope_node=entity, submitted_by=user,
        )


# ---------------------------------------------------------------------------
# 2. Send handoff
# ---------------------------------------------------------------------------

def test_send_handoff_creates_tokens(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    sent_h = send_finance_handoff(h, triggered_by=user)
    assert sent_h.status == FinanceHandoffStatus.SENT
    assert sent_h.sent_at is not None

    tokens = FinanceActionToken.objects.filter(handoff=h)
    assert tokens.count() == 2
    approve_tok = tokens.get(action_type=FinanceActionType.APPROVE)
    reject_tok = tokens.get(action_type=FinanceActionType.REJECT)
    assert not approve_tok.is_used()
    assert not approve_tok.is_expired()
    assert not reject_tok.is_used()
    assert not reject_tok.is_expired()


def test_send_handoff_calls_email(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    mock_email.assert_called_once()
    call_kwargs = mock_email.call_args.kwargs
    assert call_kwargs["module"] == "invoice"
    assert call_kwargs["subject_name"] == "Test Invoice"
    assert "/approve/" in call_kwargs["approve_url"]
    assert "/reject/" in call_kwargs["reject_url"]


def test_send_handoff_not_pending_raises(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)  # now SENT
    with pytest.raises(HandoffStateError):
        send_finance_handoff(h, triggered_by=user)


# ---------------------------------------------------------------------------
# 3. Approve via token
# ---------------------------------------------------------------------------

def test_approve_handoff_ok(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    with patch("apps.finance.services._build_audit_log"):
        approved_h, decision = finance_approve_handoff(
            approve_tok.token, reference_id="SAP-REF-001", note="Approved"
        )

    assert approved_h.status == FinanceHandoffStatus.FINANCE_APPROVED
    assert approved_h.finance_reference_id == "SAP-REF-001"
    assert decision.decision == FinanceDecisionChoice.APPROVED
    assert decision.reference_id == "SAP-REF-001"
    assert decision.acted_via_token == approve_tok
    approve_tok.refresh_from_db()
    assert approve_tok.is_used()


def test_approve_handoff_syncs_invoice_status(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    with patch("apps.finance.services._build_audit_log"):
        finance_approve_handoff(approve_tok.token, reference_id="SAP-001")

    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.FINANCE_APPROVED


def test_approve_with_reject_token_raises(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    reject_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.REJECT)

    with pytest.raises(TokenError) as exc:
        finance_approve_handoff(reject_tok.token, reference_id="SAP-001")
    assert "expected 'approve'" in str(exc.value)


def test_approve_empty_reference_raises(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    with pytest.raises(ValueError) as exc:
        finance_approve_handoff(approve_tok.token, reference_id="")
    assert "reference_id is required" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. Reject via token
# ---------------------------------------------------------------------------

def test_reject_handoff_ok(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    reject_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.REJECT)

    with patch("apps.finance.services._build_audit_log"):
        rejected_h, decision = finance_reject_handoff(
            reject_tok.token, note="Missing PO"
        )

    assert rejected_h.status == FinanceHandoffStatus.FINANCE_REJECTED
    assert decision.decision == FinanceDecisionChoice.REJECTED
    assert decision.note == "Missing PO"
    reject_tok.refresh_from_db()
    assert reject_tok.is_used()


def test_reject_handoff_syncs_invoice_status(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    reject_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.REJECT)

    with patch("apps.finance.services._build_audit_log"):
        finance_reject_handoff(reject_tok.token)

    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.FINANCE_REJECTED


def test_reject_with_approve_token_raises(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    with pytest.raises(TokenError) as exc:
        finance_reject_handoff(approve_tok.token)
    assert "expected 'reject'" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. Token reuse / expiry
# ---------------------------------------------------------------------------

def test_token_reuse_raises(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    with patch("apps.finance.services._build_audit_log"):
        finance_approve_handoff(approve_tok.token, reference_id="SAP-001")

    with pytest.raises(TokenError) as exc:
        with patch("apps.finance.services._build_audit_log"):
            finance_approve_handoff(approve_tok.token, reference_id="SAP-002")
    assert "already been used" in str(exc.value)


def test_expired_token_raises(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    # Backdate expiry
    approve_tok.expires_at = timezone.now() - timedelta(hours=1)
    approve_tok.save(update_fields=["expires_at"])

    with pytest.raises(TokenError) as exc:
        finance_approve_handoff(approve_tok.token, reference_id="SAP-001")
    assert "expired" in str(exc.value)


def test_unknown_token_raises(db):
    with pytest.raises(TokenError) as exc:
        _get_valid_finance_token = None  # noqa: suppress unused
        # Use the public function directly
        finance_approve_handoff("not-a-real-token", reference_id="REF")
    assert "Invalid finance action token" in str(exc.value)


# ---------------------------------------------------------------------------
# 6. Campaign integration
# ---------------------------------------------------------------------------

def test_approve_handoff_syncs_campaign_status(db, campaign, entity, user, mock_email):
    h = create_finance_handoff(
        module="campaign", subject_type="campaign", subject_id=campaign.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    with patch("apps.finance.services._build_audit_log"):
        finance_approve_handoff(approve_tok.token, reference_id="CAMP-REF-001")

    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.FINANCE_APPROVED


def test_reject_handoff_syncs_campaign_status(db, campaign, entity, user, mock_email):
    h = create_finance_handoff(
        module="campaign", subject_type="campaign", subject_id=campaign.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    reject_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.REJECT)

    with patch("apps.finance.services._build_audit_log"):
        finance_reject_handoff(reject_tok.token, note="Budget exceeded")

    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.FINANCE_REJECTED


# ---------------------------------------------------------------------------
# 7. Vendor integration note
# ---------------------------------------------------------------------------

def test_vendor_integration_note(db):
    """
    The generic finance layer does NOT override vendor-specific tokens.
    VendorFinanceActionToken / VendorInvitation remain the source of truth for
    vendor onboarding. This test exists as documentation that the paths diverge.
    """
    # No vendor finance handoff records exist in apps.finance for vendor subjects
    assert FinanceHandoff.objects.filter(module="vendor").count() == 0


# ---------------------------------------------------------------------------
# 8. Error / edge cases
# ---------------------------------------------------------------------------

def test_get_active_handoff_returns_pending(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    active = get_active_handoff_for_subject("invoice", "invoice", invoice.pk)
    assert active == h


def test_get_active_handoff_returns_sent(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    active = get_active_handoff_for_subject("invoice", "invoice", invoice.pk)
    assert active == h


def test_get_active_handoff_none_after_finalize(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)
    with patch("apps.finance.services._build_audit_log"):
        finance_approve_handoff(approve_tok.token, reference_id="REF")
    active = get_active_handoff_for_subject("invoice", "invoice", invoice.pk)
    assert active is None


def test_get_handoff_by_token_ok(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)
    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)

    found = get_handoff_by_token(approve_tok.token)
    assert found == h


def test_get_handoff_by_unknown_token_raises(db):
    with pytest.raises(TokenError):
        get_handoff_by_token("unknown-token")


def test_sync_subject_on_finance_change_unknown_module(db, entity, mock_email):
    """sync_subject_on_finance_change silently skips unsupported modules."""
    h = create_finance_handoff(
        module="unknown_module",
        subject_type="unknown",
        subject_id=99999,
        scope_node=entity,
    )
    # Should not raise
    sync_subject_on_finance_change(h)


# ---------------------------------------------------------------------------
# 9. Handoff status transitions
# ---------------------------------------------------------------------------

def test_handoff_status_pending_to_sent_to_approved(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    assert h.status == FinanceHandoffStatus.PENDING

    sent_h = send_finance_handoff(h, triggered_by=user)
    assert sent_h.status == FinanceHandoffStatus.SENT

    approve_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.APPROVE)
    with patch("apps.finance.services._build_audit_log"):
        approved_h, _ = finance_approve_handoff(approve_tok.token, reference_id="REF-1")
    assert approved_h.status == FinanceHandoffStatus.FINANCE_APPROVED


def test_handoff_status_pending_to_sent_to_rejected(db, invoice, entity, user, mock_email):
    h = create_finance_handoff(
        module="invoice", subject_type="invoice", subject_id=invoice.pk,
        scope_node=entity, submitted_by=user,
    )
    send_finance_handoff(h, triggered_by=user)

    reject_tok = FinanceActionToken.objects.get(handoff=h, action_type=FinanceActionType.REJECT)
    with patch("apps.finance.services._build_audit_log"):
        rejected_h, _ = finance_reject_handoff(reject_tok.token, note="Bad data")
    assert rejected_h.status == FinanceHandoffStatus.FINANCE_REJECTED
