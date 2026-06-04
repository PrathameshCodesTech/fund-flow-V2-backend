from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes


User = get_user_model()


def _build_audit_log(user, action: str, resource_type: str, resource_id: int, metadata: dict | None = None) -> None:
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
        pass


def can_admin_reset_password(actor) -> bool:
    if not actor or not actor.is_authenticated:
        return False
    if actor.is_superuser:
        return True
    from apps.access.capabilities import get_user_capabilities

    capabilities = set(get_user_capabilities(actor))
    return "iam.manage" in capabilities


def send_password_reset_for_user(*, target_user, requested_by) -> dict:
    if not target_user.email:
        raise ValueError("User has no email address.")

    uid = urlsafe_base64_encode(force_bytes(target_user.pk))
    token = default_token_generator.make_token(target_user)
    base_url = getattr(settings, "FUND_FLOW_BASE_URL", "http://localhost:3000").rstrip("/")
    reset_url = f"{base_url}/password-reset/{uid}/{token}"

    from apps.users.email import send_internal_password_reset_email

    requested_by_name = requested_by.get_full_name() if requested_by else ""
    send_internal_password_reset_email(
        email=target_user.email,
        user_name=target_user.get_full_name(),
        reset_url=reset_url,
        requested_by_name=requested_by_name,
    )

    _build_audit_log(
        user=requested_by,
        action="internal_user_password_reset_sent",
        resource_type="User",
        resource_id=target_user.pk,
        metadata={"target_email": target_user.email},
    )

    return {"email": target_user.email}
