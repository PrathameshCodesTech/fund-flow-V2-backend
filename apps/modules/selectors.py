from apps.modules.models import ModuleActivation


def get_activations_for_node(scope_node):
    return ModuleActivation.objects.filter(scope_node=scope_node).order_by("module")


def get_activations_for_module(module):
    return ModuleActivation.objects.filter(module=module).select_related("scope_node")
