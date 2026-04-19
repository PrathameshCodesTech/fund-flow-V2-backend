from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.modules.api.views import ModuleActivationViewSet, ModuleActivationResolveView

router = DefaultRouter()
router.register("activations", ModuleActivationViewSet, basename="moduleactivation")

urlpatterns = [
    path("", include(router.urls)),
    path("resolve/", ModuleActivationResolveView.as_view(), name="module-resolve"),
]
