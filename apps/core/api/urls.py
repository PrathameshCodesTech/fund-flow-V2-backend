from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.core.api.views import OrganizationViewSet, ScopeNodeViewSet

router = DefaultRouter()
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("nodes", ScopeNodeViewSet, basename="scopenode")

urlpatterns = [
    path("", include(router.urls)),
]
