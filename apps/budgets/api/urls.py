from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.budgets.api.views import (
    BudgetCategoryViewSet,
    BudgetSubCategoryViewSet,
    BudgetViewSet,
    BudgetLineViewSet,
    BudgetRuleViewSet,
    BudgetConsumptionViewSet,
    BudgetVarianceRequestViewSet,
    BudgetOverviewView,
)

# Register specific prefixes first so they are matched before the greedy
# BudgetViewSet detail pattern (pk='rules' etc. shadowing).
router = DefaultRouter()
router.register("categories", BudgetCategoryViewSet, basename="budgetcategory")
router.register("subcategories", BudgetSubCategoryViewSet, basename="budgetsubcategory")
router.register("lines", BudgetLineViewSet, basename="budgetline")
router.register("rules", BudgetRuleViewSet, basename="budgetrule")
router.register("consumptions", BudgetConsumptionViewSet, basename="budgetconsumption")
router.register("variance-requests", BudgetVarianceRequestViewSet, basename="budgetvariancerequest")
router.register("", BudgetViewSet, basename="budget")

urlpatterns = [
    path("overview/", BudgetOverviewView.as_view(), name="budgets-overview"),
    path("", include(router.urls)),
]
