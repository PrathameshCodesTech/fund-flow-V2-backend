from django.urls import path
from apps.dashboard.api.views import OpsDashboardView, InsightsView

urlpatterns = [
    path("ops/", OpsDashboardView.as_view(), name="ops-dashboard"),
    path("insights/", InsightsView.as_view(), name="insights"),
]
