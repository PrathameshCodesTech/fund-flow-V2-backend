from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.campaigns.api.views import CampaignViewSet, CampaignDocumentViewSet

# Register specific prefixes first to prevent the greedy Campaign detail
# pattern from shadowing them.
router = DefaultRouter()
router.register("documents", CampaignDocumentViewSet, basename="campaigndocument")
router.register("", CampaignViewSet, basename="campaign")

urlpatterns = [
    path("", include(router.urls)),
]
