from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.workflow.models import WorkflowSplitOption
from apps.access.selectors import get_user_visible_scope_ids
from apps.access.services import user_can_act_on_scope_response
from apps.workflow.api.serializers.split_options import WorkflowSplitOptionSerializer


class WorkflowSplitOptionViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowSplitOptionSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowSplitOption.objects.select_related(
            "workflow_step", "entity", "approver_role",
            "category", "subcategory", "campaign", "budget"
        ).filter(
            workflow_step__group__template_version__template__scope_node_id__in=visible_scope_ids
        )
        step_id = self.request.query_params.get("workflow_step")
        if step_id:
            qs = qs.filter(workflow_step_id=step_id)
        return qs

    def _get_scope_node_id_from_step(self, step_id):
        from apps.workflow.models import WorkflowStep
        step = WorkflowStep.objects.select_related(
            "group__template_version__template"
        ).get(pk=step_id)
        return step.group.template_version.template.scope_node_id

    def create(self, request, *args, **kwargs):
        step_id = request.data.get("workflow_step")
        if not step_id:
            return Response({"detail": "workflow_step is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            scope_node_id = self._get_scope_node_id_from_step(step_id)
        except Exception as e:
            return Response({"detail": f"workflow_step {step_id} not found."}, status=status.HTTP_400_BAD_REQUEST)
        if err := user_can_act_on_scope_response(request.user, scope_node_id, "create split options"):
            return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        opt = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id_from_step(opt.workflow_step_id), "update split option"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        opt = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id_from_step(opt.workflow_step_id), "update split option"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        opt = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id_from_step(opt.workflow_step_id), "delete split option"):
            return err
        return super().destroy(request, *args, **kwargs)
