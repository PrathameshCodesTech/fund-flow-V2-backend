from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Q

from apps.users.models import User
from apps.users.api.serializers.users import (
    UserSerializer,
    UserListSerializer,
    UserCreateSerializer,
    UserUpdateSerializer,
)


class IsAdminOrReadOnly:
    """
    Allow read access to any authenticated user.
    Write access (create/update) requires admin (is_staff=True).
    """

    def has_permission(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return request.user and request.user.is_authenticated
        if getattr(view, "action", None) == "send_password_reset":
            return request.user and request.user.is_authenticated
        return request.user and request.user.is_authenticated and request.user.is_staff

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view)


class UserViewSet(viewsets.ModelViewSet):
    """
    User CRUD endpoint.

    Read operations are available to any authenticated user:
        GET /api/v1/users/           — paginated list
        GET /api/v1/users/?q=alice   — search by email, first_name, last_name
        GET /api/v1/users/?is_active=true — filter by active status
        GET /api/v1/users/{id}/      — retrieve single user

    Write operations require admin (is_staff=True):
        POST /api/v1/users/          — create user
        PATCH /api/v1/users/{id}/    — partial update (name, employee_id, is_active)
    """

    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    serializer_class = UserSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["email", "first_name", "last_name"]
    ordering_fields = ["id", "email", "first_name", "date_joined"]
    ordering = ["id"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = User.objects.all()
        q = self.request.query_params.get("q")
        is_active = self.request.query_params.get("is_active")
        user_type = (self.request.query_params.get("user_type") or "").strip().lower()

        if q:
            qs = qs.filter(
                Q(email__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )
        if is_active is not None:
            is_active_bool = is_active.lower() in ("true", "1", "yes")
            qs = qs.filter(is_active=is_active_bool)
        if user_type == "vendor":
            qs = qs.filter(vendor_assignments__is_active=True).distinct()
        elif user_type == "internal":
            qs = qs.exclude(vendor_assignments__is_active=True).distinct()

        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action in ("partial_update", "update"):
            return UserUpdateSerializer
        if self.action == "list":
            return UserListSerializer
        return UserSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            UserSerializer(user).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="send-password-reset")
    def send_password_reset(self, request, pk=None):
        from apps.users.services import can_admin_reset_password, send_password_reset_for_user

        if not can_admin_reset_password(request.user):
            return Response(
                {"detail": "You do not have permission to send password reset emails."},
                status=status.HTTP_403_FORBIDDEN,
            )

        user = self.get_object()
        try:
            result = send_password_reset_for_user(target_user=user, requested_by=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response(
                {"detail": f"Failed to send password reset email: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "detail": "Password reset email sent.",
            **result,
        })
