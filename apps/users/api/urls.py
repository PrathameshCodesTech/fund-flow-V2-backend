from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.users.api.views.auth import LoginView, MeView
from apps.users.api.views.users import UserViewSet

# Auth URLs at /api/v1/auth/...
auth_urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
]

# User list/search at /api/v1/users/...
router = DefaultRouter()
router.register("users", UserViewSet, basename="users")

# Combined: auth at /api/v1/auth/ + users at /api/v1/users/
urlpatterns = auth_urlpatterns + router.urls
