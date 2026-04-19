from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.finance.api.views import (
    FinanceHandoffViewSet,
    PublicFinanceApproveView,
    PublicFinanceRejectView,
    PublicFinanceTokenView,
)

router = DefaultRouter()
router.register("handoffs", FinanceHandoffViewSet, basename="financehandoff")

urlpatterns = [
    path("", include(router.urls)),

    # Public token-based endpoints
    path(
        "public/<str:token>/",
        PublicFinanceTokenView.as_view(),
        name="public-finance-token",
    ),
    path(
        "public/<str:token>/approve/",
        PublicFinanceApproveView.as_view(),
        name="public-finance-approve",
    ),
    path(
        "public/<str:token>/reject/",
        PublicFinanceRejectView.as_view(),
        name="public-finance-reject",
    ),
]
