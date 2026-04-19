from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.access.api.views.roles import RoleViewSet, PermissionViewSet, RolePermissionViewSet
from apps.access.api.views.assignments import UserScopeAssignmentViewSet, UserRoleAssignmentViewSet

router = DefaultRouter()
router.register("roles", RoleViewSet, basename="role")
router.register("permissions", PermissionViewSet, basename="permission")
router.register("role-permissions", RolePermissionViewSet, basename="rolepermission")
router.register("scope-assignments", UserScopeAssignmentViewSet, basename="scopeassignment")
router.register("role-assignments", UserRoleAssignmentViewSet, basename="roleassignment")

urlpatterns = [
    path("", include(router.urls)),
]
