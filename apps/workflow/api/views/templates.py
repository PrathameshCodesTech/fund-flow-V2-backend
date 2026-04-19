from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from apps.workflow.models import (
    WorkflowTemplate,
    WorkflowTemplateVersion,
    StepGroup,
    WorkflowStep,
)
from apps.workflow.api.serializers.templates import (
    WorkflowTemplateSerializer,
    WorkflowTemplateVersionSerializer,
    StepGroupSerializer,
    WorkflowStepSerializer,
)
from apps.workflow.services import publish_template_version, archive_template_version
from apps.access.selectors import get_user_visible_scope_ids
from apps.access.services import user_can_act_on_scope_response


class WorkflowTemplateViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowTemplateSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowTemplate.objects.select_related("scope_node", "created_by").prefetch_related(
            "versions"
        ).filter(scope_node_id__in=visible_scope_ids)
        node_id = self.request.query_params.get("scope_node")
        module = self.request.query_params.get("module")
        if node_id:
            qs = qs.filter(scope_node_id=node_id)
        if module:
            qs = qs.filter(module=module)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def create(self, request, *args, **kwargs):
        scope_node_id = request.data.get("scope_node")
        if scope_node_id:
            if err := user_can_act_on_scope_response(request.user, scope_node_id, "create a workflow template"):
                return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        template = self.get_object()
        if err := user_can_act_on_scope_response(request.user, template.scope_node_id, "update this workflow template"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        template = self.get_object()
        if err := user_can_act_on_scope_response(request.user, template.scope_node_id, "update this workflow template"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        template = self.get_object()
        if err := user_can_act_on_scope_response(request.user, template.scope_node_id, "delete this workflow template"):
            return err
        return super().destroy(request, *args, **kwargs)


class WorkflowTemplateVersionViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowTemplateVersionSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowTemplateVersion.objects.select_related("template").prefetch_related(
            "step_groups__steps"
        ).filter(template__scope_node_id__in=visible_scope_ids)
        template_id = self.request.query_params.get("template")
        if template_id:
            qs = qs.filter(template_id=template_id)
        return qs

    def _get_template_scope_node_id(self, obj):
        return obj.template.scope_node_id

    def create(self, request, *args, **kwargs):
        template_id = request.data.get("template")
        if template_id:
            try:
                template = WorkflowTemplate.objects.get(pk=template_id)
                if err := user_can_act_on_scope_response(request.user, template.scope_node_id, "create a template version"):
                    return err
            except WorkflowTemplate.DoesNotExist:
                pass
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        version = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_template_scope_node_id(version), "update this template version"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        version = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_template_scope_node_id(version), "update this template version"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        version = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_template_scope_node_id(version), "delete this template version"):
            return err
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        version = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_template_scope_node_id(version), "publish this template version"):
            return err
        try:
            version = publish_template_version(version, published_by=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowTemplateVersionSerializer(version).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        version = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_template_scope_node_id(version), "archive this template version"):
            return err
        try:
            version = archive_template_version(version)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowTemplateVersionSerializer(version).data)


class StepGroupViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = StepGroupSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = StepGroup.objects.select_related("template_version").prefetch_related(
            "steps"
        ).filter(template_version__template__scope_node_id__in=visible_scope_ids)
        version_id = self.request.query_params.get("template_version")
        if version_id:
            qs = qs.filter(template_version_id=version_id)
        return qs.order_by("display_order")

    def _get_scope_node_id(self, obj):
        return obj.template_version.template.scope_node_id

    def create(self, request, *args, **kwargs):
        version_id = request.data.get("template_version")
        if version_id:
            try:
                version = WorkflowTemplateVersion.objects.select_related("template").get(pk=version_id)
                if err := user_can_act_on_scope_response(request.user, version.template.scope_node_id, "create a step group"):
                    return err
            except WorkflowTemplateVersion.DoesNotExist:
                pass
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        group = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id(group), "update this step group"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        group = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id(group), "update this step group"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        group = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id(group), "delete this step group"):
            return err
        return super().destroy(request, *args, **kwargs)


class WorkflowStepViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowStepSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowStep.objects.select_related("group", "required_role").filter(
            group__template_version__template__scope_node_id__in=visible_scope_ids
        )
        group_id = self.request.query_params.get("group")
        if group_id:
            qs = qs.filter(group_id=group_id)
        return qs.order_by("display_order")

    def _get_scope_node_id(self, obj):
        return obj.group.template_version.template.scope_node_id

    def create(self, request, *args, **kwargs):
        group_id = request.data.get("group")
        if group_id:
            try:
                group = StepGroup.objects.select_related("template_version__template").get(pk=group_id)
                if err := user_can_act_on_scope_response(request.user, group.template_version.template.scope_node_id, "create a workflow step"):
                    return err
            except StepGroup.DoesNotExist:
                pass
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        step = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id(step), "update this workflow step"):
            return err
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        step = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id(step), "update this workflow step"):
            return err
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        step = self.get_object()
        if err := user_can_act_on_scope_response(request.user, self._get_scope_node_id(step), "delete this workflow step"):
            return err
        return super().destroy(request, *args, **kwargs)
