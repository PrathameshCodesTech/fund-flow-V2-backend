from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.manual_expenses.api.views import (
    ManualExpenseViewSet,
    ManualExpenseAttachmentViewSet,
)

router = DefaultRouter()
router.register("expenses", ManualExpenseViewSet, basename="manual-expense")
router.register("expense-attachments", ManualExpenseAttachmentViewSet, basename="manual-expense-attachment")

urlpatterns = [
    path("", include(router.urls)),
]