from django.contrib import admin
from .models import ModuleActivation


@admin.register(ModuleActivation)
class ModuleActivationAdmin(admin.ModelAdmin):
    list_display = ("module", "scope_node", "is_active", "override_parent", "updated_at")
    list_filter = ("module", "is_active", "override_parent")
    raw_id_fields = ("scope_node",)
