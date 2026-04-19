from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/v1/", include("apps.users.api.urls")),
    path("api/v1/core/", include("apps.core.api.urls")),
    path("api/v1/access/", include("apps.access.api.urls")),
    path("api/v1/modules/", include("apps.modules.api.urls")),
    path("api/v1/workflow/", include("apps.workflow.api.urls")),
    path("api/v1/invoices/", include("apps.invoices.api.urls")),
    path("api/v1/notifications/", include("apps.notifications.api.urls")),
    path("api/v1/campaigns/", include("apps.campaigns.api.urls")),
    path("api/v1/vendors/", include("apps.vendors.api.urls")),
    path("api/v1/budgets/", include("apps.budgets.api.urls")),
    path("api/v1/finance/", include("apps.finance.api.urls")),
    path("api/v1/dashboard/", include("apps.dashboard.api.urls")),
]
