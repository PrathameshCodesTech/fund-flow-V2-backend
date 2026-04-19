from django.contrib import admin
from .models import Role, Permission, RolePermission, UserScopeAssignment, UserRoleAssignment


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "org", "node_type_scope", "is_active")
    list_filter = ("org", "is_active")
    search_fields = ("code", "name")


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("action", "resource", "description")
    list_filter = ("action", "resource")


@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    list_display = ("role", "permission")
    list_filter = ("role__org",)
    raw_id_fields = ("role", "permission")


@admin.register(UserScopeAssignment)
class UserScopeAssignmentAdmin(admin.ModelAdmin):
    list_display = ("user", "scope_node", "assignment_type", "created_at")
    list_filter = ("assignment_type",)
    raw_id_fields = ("user", "scope_node")


@admin.register(UserRoleAssignment)
class UserRoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "scope_node", "created_at")
    list_filter = ("role__org",)
    raw_id_fields = ("user", "role", "scope_node")
