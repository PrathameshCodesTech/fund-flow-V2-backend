from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.workflow.api.views.templates import (
    WorkflowTemplateViewSet,
    WorkflowTemplateVersionViewSet,
    StepGroupViewSet,
    WorkflowStepViewSet,
)
from apps.workflow.api.views.instances import (
    WorkflowInstanceViewSet,
    WorkflowInstanceGroupViewSet,
    WorkflowInstanceStepViewSet,
    WorkflowInstanceBranchViewSet,
    MyTasksView,
    TaskReviewView,
)

router = DefaultRouter()
router.register("templates", WorkflowTemplateViewSet, basename="workflowtemplate")
router.register("versions", WorkflowTemplateVersionViewSet, basename="workflowtemplateversion")
router.register("groups", StepGroupViewSet, basename="stepgroup")
router.register("steps", WorkflowStepViewSet, basename="workflowstep")
router.register("instances", WorkflowInstanceViewSet, basename="workflowinstance")
router.register("instance-groups", WorkflowInstanceGroupViewSet, basename="workflowinstancegroup")
router.register("instance-steps", WorkflowInstanceStepViewSet, basename="workflowinstancestep")
router.register("branches", WorkflowInstanceBranchViewSet, basename="workflowinstancebranch")

urlpatterns = [
    path("", include(router.urls)),
    path("tasks/me/", MyTasksView.as_view(), name="workflow-my-tasks"),
    path("tasks/<str:task_kind>/<int:pk>/review/", TaskReviewView.as_view(), name="workflow-task-review"),
]
