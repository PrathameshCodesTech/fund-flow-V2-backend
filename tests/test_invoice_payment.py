"""
Tests for invoice payment recording — post-finance payment visibility layer.
"""
import pytest
from decimal import Decimal
from datetime import date

from django.contrib.auth import get_user_model

from apps.core.models import Organization, ScopeNode, NodeType
from apps.invoices.models import (
    Invoice, InvoiceStatus, InvoicePayment,
    InvoicePaymentStatus, PaymentMethod,
)
from apps.invoices.services import (
    record_invoice_payment,
    can_user_record_invoice_payment,
    PaymentPermissionError,
    PaymentValidationError,
    get_or_create_invoice_payment,
)


User = get_user_model()


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Acme Corp", code="acme")


@pytest.fixture
def company(org):
    return ScopeNode.objects.create(
        org=org, parent=None, name="HQ", code="hq",
        node_type=NodeType.COMPANY, path="/acme/hq", depth=0,
    )


@pytest.fixture
def creator(db):
    return User.objects.create_user(email="creator@acme.com", password="pass")


@pytest.fixture
def admin_user(db):
    user = User.objects.create_user(email="admin@acme.com", password="pass")
    user.is_superuser = True
    user.save()
    return user


def _finance_approved_invoice(company, creator):
    return Invoice.objects.create(
        scope_node=company, title="Invoice F", amount=Decimal("10000.00"),
        currency="INR", status=InvoiceStatus.FINANCE_APPROVED, created_by=creator,
    )


def _paid_invoice(company, creator):
    return Invoice.objects.create(
        scope_node=company, title="Invoice X", amount=Decimal("10000.00"),
        currency="INR", status=InvoiceStatus.PAID, created_by=creator,
    )


# ---------------------------------------------------------------------------
# can_user_record_invoice_payment
# ---------------------------------------------------------------------------

class TestCanUserRecordPayment:
    def test_false_for_draft(self, org, company, creator):
        inv = Invoice.objects.create(
            scope_node=company, title="D", amount=Decimal("1000"),
            currency="INR", status=InvoiceStatus.DRAFT, created_by=creator,
        )
        assert can_user_record_invoice_payment(creator, inv) is False

    def test_false_for_pending_workflow(self, org, company, creator):
        inv = Invoice.objects.create(
            scope_node=company, title="P", amount=Decimal("1000"),
            currency="INR", status=InvoiceStatus.PENDING_WORKFLOW, created_by=creator,
        )
        assert can_user_record_invoice_payment(creator, inv) is False

    def test_false_for_paid(self, org, company, creator):
        inv = _paid_invoice(company, creator)
        assert can_user_record_invoice_payment(creator, inv) is False

    def test_true_for_finance_approved_creator(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        assert can_user_record_invoice_payment(creator, inv) is True

    def test_true_for_superuser(self, org, company, admin_user):
        inv = _finance_approved_invoice(company, admin_user)
        assert can_user_record_invoice_payment(admin_user, inv) is True

    def test_false_for_unrelated_user(self, org, company, creator):
        unrelated = User.objects.create_user(email="other@acme.com", password="pass")
        inv = _finance_approved_invoice(company, creator)
        assert can_user_record_invoice_payment(unrelated, inv) is False


# ---------------------------------------------------------------------------
# record_invoice_payment — basic creation
# ---------------------------------------------------------------------------

class TestRecordPaymentBasic:
    def test_raises_when_not_eligible(self, org, company, creator):
        inv = Invoice.objects.create(
            scope_node=company, title="D", amount=Decimal("1000"),
            currency="INR", status=InvoiceStatus.DRAFT, created_by=creator,
        )
        with pytest.raises(PaymentValidationError) as exc_info:
            record_invoice_payment(inv, creator, {"payment_status": "paid"})
        assert "finance approval" in str(exc_info.value)

    def test_admin_can_record_pending(self, org, company, admin_user):
        inv = _finance_approved_invoice(company, admin_user)
        payment = record_invoice_payment(inv, admin_user, {})
        assert payment.id is not None
        assert payment.payment_status == InvoicePaymentStatus.PENDING

    def test_creator_can_record_pending(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        payment = record_invoice_payment(inv, creator, {})
        assert payment.id is not None
        assert payment.payment_status == InvoicePaymentStatus.PENDING


# ---------------------------------------------------------------------------
# Mark PAID requires correct fields
# ---------------------------------------------------------------------------

class TestMarkAsPaidRequiresFields:
    def test_missing_payment_method_raises(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        with pytest.raises(PaymentValidationError) as exc_info:
            record_invoice_payment(inv, creator, {
                "payment_status": "paid",
                "payment_date": date(2025, 4, 10),
                "paid_amount": Decimal("10000.00"),
                "payment_reference_number": "REF-001",
            })
        assert isinstance(exc_info.value.args[0], dict)
        assert "payment_method" in exc_info.value.args[0]

    def test_missing_payment_date_raises(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        with pytest.raises(PaymentValidationError) as exc_info:
            record_invoice_payment(inv, creator, {
                "payment_status": "paid",
                "payment_method": "bank_transfer",
                "paid_amount": Decimal("10000.00"),
                "utr_number": "UTR-001",
            })
        assert isinstance(exc_info.value.args[0], dict)
        assert "payment_date" in exc_info.value.args[0]

    def test_zero_amount_raises(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        with pytest.raises(PaymentValidationError) as exc_info:
            record_invoice_payment(inv, creator, {
                "payment_status": "paid",
                "payment_method": "rtgs",
                "payment_date": date(2025, 4, 10),
                "paid_amount": Decimal("0.00"),
                "payment_reference_number": "REF-001",
            })
        assert isinstance(exc_info.value.args[0], dict)
        assert "paid_amount" in exc_info.value.args[0]

    def test_missing_ref_and_utr_raises(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        with pytest.raises(PaymentValidationError) as exc_info:
            record_invoice_payment(inv, creator, {
                "payment_status": "paid",
                "payment_method": "neft",
                "payment_date": date(2025, 4, 10),
                "paid_amount": Decimal("10000.00"),
            })
        assert isinstance(exc_info.value.args[0], dict)
        assert any(k in exc_info.value.args[0] for k in ("utr_number", "payment_reference_number"))

    def test_valid_paid_succeeds(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        payment = record_invoice_payment(inv, creator, {
            "payment_status": "paid",
            "payment_method": "rtgs",
            "payment_date": date(2025, 4, 10),
            "paid_amount": Decimal("10000.00"),
            "utr_number": "UTR-001",
            "remarks": "Payment received",
        })
        assert payment.payment_status == InvoicePaymentStatus.PAID
        assert payment.payment_method == PaymentMethod.RTGS
        assert payment.recorded_by == creator

    def test_ref_number_alone_is_sufficient(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        payment = record_invoice_payment(inv, creator, {
            "payment_status": "paid",
            "payment_method": "upi",
            "payment_date": date(2025, 4, 10),
            "paid_amount": Decimal("5000.00"),
            "payment_reference_number": "REF-ONLY",
        })
        assert payment.payment_status == InvoicePaymentStatus.PAID


# ---------------------------------------------------------------------------
# Payment update + invoice status
# ---------------------------------------------------------------------------

class TestPaymentUpdateAndInvoiceStatus:
    def test_second_call_updates_existing(self, org, company, creator, admin_user):
        inv = _finance_approved_invoice(company, creator)
        p1 = record_invoice_payment(inv, creator, {"remarks": "Initial"})
        assert p1.payment_status == InvoicePaymentStatus.PENDING

        p2 = record_invoice_payment(
            inv, admin_user,
            {
                "payment_status": "paid",
                "payment_method": "neft",
                "payment_date": date(2025, 4, 15),
                "paid_amount": Decimal("10000.00"),
                "utr_number": "UTRUPD",
                "remarks": "Updated",
            },
        )
        assert p2.pk == p1.pk
        assert p2.payment_status == InvoicePaymentStatus.PAID
        assert p2.updated_by == admin_user
        assert p2.recorded_by == creator  # preserved from first record

    def test_marking_paid_updates_invoice_status(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        record_invoice_payment(
            inv, creator,
            {
                "payment_status": "paid",
                "payment_method": "bank_transfer",
                "payment_date": date(2025, 4, 10),
                "paid_amount": Decimal("10000.00"),
                "payment_reference_number": "REF-001",
            },
        )
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.PAID


# ---------------------------------------------------------------------------
# get_or_create_invoice_payment
# ---------------------------------------------------------------------------

class TestGetOrCreatePayment:
    def test_creates_new_pending(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        payment = get_or_create_invoice_payment(inv)
        assert payment.id is not None
        assert payment.invoice == inv
        assert payment.payment_status == InvoicePaymentStatus.PENDING

    def test_returns_existing(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        existing = InvoicePayment.objects.create(
            invoice=inv, payment_status=InvoicePaymentStatus.PENDING,
        )
        payment = get_or_create_invoice_payment(inv)
        assert payment.pk == existing.pk


# ---------------------------------------------------------------------------
# All reference fields stored correctly
# ---------------------------------------------------------------------------

class TestPaymentRecordFields:
    def test_all_reference_fields_stored(self, org, company, creator):
        inv = _finance_approved_invoice(company, creator)
        payment = record_invoice_payment(
            inv, creator,
            {
                "payment_status": "paid",
                "payment_method": "cheque",
                "payment_reference_number": "CHEQUE-001",
                "utr_number": "UTR-002",
                "transaction_id": "TXN-003",
                "bank_reference_number": "BNK-REF",
                "paid_amount": Decimal("7500.00"),
                "currency": "INR",
                "payment_date": date(2025, 4, 20),
                "remarks": "Cheque deposited",
            },
        )
        assert payment.payment_reference_number == "CHEQUE-001"
        assert payment.utr_number == "UTR-002"
        assert payment.transaction_id == "TXN-003"
        assert payment.bank_reference_number == "BNK-REF"
        assert payment.remarks == "Cheque deposited"

    def test_internal_bank_fields_stored(self, org, company, admin_user):
        inv = _finance_approved_invoice(company, admin_user)
        payment = record_invoice_payment(
            inv, admin_user,
            {
                "payer_bank_name": "HDFC Bank",
                "beneficiary_name": "Acme Corp",
                "beneficiary_bank_name": "ICICI Bank",
                "payment_status": "paid",
                "payment_method": "bank_transfer",
                "payment_date": date(2025, 4, 10),
                "paid_amount": Decimal("10000.00"),
                "utr_number": "UTR-HDFC",
            },
        )
        assert payment.payer_bank_name == "HDFC Bank"
        assert payment.beneficiary_name == "Acme Corp"
        assert payment.beneficiary_bank_name == "ICICI Bank"