from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.views import APIView
from django.db.models import Q

from apps.workflow.models import (
    WorkflowInstance, WorkflowInstanceGroup, WorkflowInstanceStep, InstanceStatus,
    WorkflowInstanceBranch, BranchStatus, StepKind,
)
from apps.workflow.api.serializers.instances import (
    WorkflowInstanceSerializer,
    WorkflowInstanceCreateSerializer,
    WorkflowInstanceGroupSerializer,
    WorkflowInstanceStepSerializer,
    WorkflowInstanceBranchSerializer,
    AssignmentPlanSerializer,
)
from apps.workflow.services import (
    create_workflow_instance_draft,
    activate_workflow_instance,
    resolve_workflow_template_version,
    apply_step_assignment_overrides,
    approve_workflow_step,
    reject_workflow_step,
    reassign_workflow_step,
    approve_workflow_branch,
    reject_workflow_branch,
    reassign_workflow_branch,
    StepActionError,
    ModuleInactiveError,
    WorkflowNotConfiguredError,
    get_eligible_users_for_step,
    resolve_step_target_node,
)
from apps.access.selectors import get_user_actionable_scope_ids, get_user_visible_scope_ids
from apps.access.services import user_can_act_on_scope_response
from apps.workflow.selectors import get_pending_tasks_for_user
from django.contrib.auth import get_user_model


class WorkflowInstanceViewSet(ModelViewSet):
    """
    WorkflowInstance endpoints.

    Actions:
        POST   /instances/                      — create draft (generic)
        GET    /instances/                      — list
        GET    /instances/{id}/                — detail
        POST   /instances/{id}/activate/       — activate DRAFT instance
        POST   /instances/from-invoice/        — create draft for invoice
    """
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == "create":
            return WorkflowInstanceCreateSerializer
        return WorkflowInstanceSerializer

    def get_queryset(self):
        # Visibility = subtree: user sees instances whose subject scope node is in their visible set.
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowInstance.objects.select_related(
            "template_version", "subject_scope_node", "current_group"
        ).prefetch_related("instance_groups__instance_steps").filter(
            subject_scope_node_id__in=visible_scope_ids
        )
        subject_type = self.request.query_params.get("subject_type")
        subject_id = self.request.query_params.get("subject_id")
        inst_status = self.request.query_params.get("status")
        if subject_type:
            qs = qs.filter(subject_type=subject_type)
        if subject_id:
            qs = qs.filter(subject_id=subject_id)
        if inst_status:
            qs = qs.filter(status=inst_status)
        return qs.order_by("-created_at")

    def create(self, request, *args, **kwargs):
        """Generic draft creation — used for non-invoice subjects or direct use."""
        subject_scope_node_id = request.data.get("subject_scope_node")
        if subject_scope_node_id:
            if err := user_can_act_on_scope_response(request.user, subject_scope_node_id, "create a workflow instance"):
                return err
        serializer = WorkflowInstanceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        instance = create_workflow_instance_draft(
            template_version=data["template_version"],
            subject_type=data["subject_type"],
            subject_id=data["subject_id"],
            subject_scope_node=data["subject_scope_node"],
            started_by=request.user,
        )
        return Response(
            WorkflowInstanceSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        """POST /instances/{id}/activate/ — activate a DRAFT instance."""
        instance = self.get_object()
        # Actionable scope check: user must have direct assignment at instance's subject scope
        if err := user_can_act_on_scope_response(request.user, instance.subject_scope_node_id, "activate this workflow"):
            return err
        try:
            instance = activate_workflow_instance(instance, activated_by=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowInstanceSerializer(instance).data)

    @action(detail=True, methods=["get"], url_path="assignment-plan")
    def assignment_plan(self, request, pk=None):
        """
        GET /instances/{id}/assignment-plan/
        Returns the full draft assignment data for one workflow instance.
        Includes groups, steps, current assignments, and eligible users.
        """
        instance = self.get_object()
        User = get_user_model()

        groups_data = []
        for ig in instance.instance_groups.order_by("display_order").select_related("step_group"):
            steps_data = []
            for ist in ig.instance_steps.order_by("workflow_step__display_order"):
                ws = ist.workflow_step
                if ws.step_kind == StepKind.SPLIT_BY_SCOPE:
                    target_node = instance.subject_scope_node
                    eligible = []
                else:
                    target_node = resolve_step_target_node(ws, instance.subject_scope_node)
                    eligible = get_eligible_users_for_step(ws, instance.subject_scope_node)

                def _user_dict(u):
                    return {
                        "id": u.id,
                        "email": u.email,
                        "first_name": u.first_name,
                        "last_name": u.last_name,
                    }

                assigned = None
                if ist.assigned_user:
                    assigned = _user_dict(ist.assigned_user)

                steps_data.append({
                    "instance_step_id": ist.id,
                    "workflow_step_id": ws.id,
                    "step_name": ws.name,
                    "step_kind": ws.step_kind,
                    "group_name": ig.step_group.name,
                    "group_display_order": ig.display_order,
                    "step_display_order": ws.display_order,
                    "assigned_user": assigned,
                    "assignment_state": ist.assignment_state,
                    "required_role": ws.required_role.name,
                    "required_role_id": ws.required_role_id,
                    "scope_resolution_policy": ws.scope_resolution_policy,
                    "resolved_scope_node_id": target_node.id if target_node else None,
                    "resolved_scope_node_name": target_node.name if target_node else None,
                    "eligible_users": [_user_dict(u) for u in eligible],
                })

            groups_data.append({
                "instance_group_id": ig.id,
                "step_group_id": ig.step_group_id,
                "name": ig.step_group.name,
                "display_order": ig.display_order,
                "steps": steps_data,
            })

        data = {
            "instance_id": instance.id,
            "status": instance.status,
            "subject_type": instance.subject_type,
            "subject_id": instance.subject_id,
            "groups": groups_data,
        }
        return Response(AssignmentPlanSerializer(data).data)

    @action(detail=False, methods=["post"], url_path="from-invoice")
    def from_invoice(self, request):
        """
        POST /instances/from-invoice/

        INVOICE WORKFLOW IS NO LONGER SUPPORTED VIA THIS ENDPOINT.
        Invoice workflow must be attached explicitly via:
            POST /api/v1/invoices/{id}/attach-workflow/

        This endpoint now returns 410 Gone for all invoice subject types.
        """
        invoice_id = request.data.get("invoice_id")
        if invoice_id:
            from apps.invoices.models import Invoice
            if Invoice.objects.filter(pk=invoice_id).exists():
                return Response(
                    {
                        "detail": (
                            "Invoice workflow can no longer be started via this endpoint. "
                            "Use POST /api/v1/invoices/{id}/attach-workflow/ instead."
                        )
                    },
                    status=status.HTTP_410_GONE,
                )

        return Response(
            {"detail": "invoice_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

        if not invoice_id:
            return Response(
                {"detail": "invoice_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.invoices.models import Invoice
        try:
            invoice = Invoice.objects.select_related("scope_node").get(pk=invoice_id)
        except Invoice.DoesNotExist:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.access.models import PermissionAction, PermissionResource
        from apps.access.services import user_has_permission_including_ancestors
        can_start = (
            invoice.created_by == request.user
            or user_has_permission_including_ancestors(
                request.user,
                PermissionAction.START_WORKFLOW,
                PermissionResource.INVOICE,
                invoice.scope_node,
            )
        )
        if not can_start:
            return Response(
                {"detail": "You do not have permission to start a workflow for this invoice."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            template_version = resolve_workflow_template_version(
                module="invoice",
                scope_node=invoice.scope_node,
            )
        except ModuleInactiveError as e:
            return Response({"detail": str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except WorkflowNotConfiguredError as e:
            return Response({"detail": str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        instance = create_workflow_instance_draft(
            template_version=template_version,
            subject_type="invoice",
            subject_id=invoice.pk,
            subject_scope_node=invoice.scope_node,
            started_by=request.user,
        )

        if assignments:
            try:
                apply_step_assignment_overrides(instance, assignments, invoice.scope_node)
            except ValueError as e:
                return Response(
                    {"detail": f"Assignment override failed: {e}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if activate_after:
            try:
                instance = activate_workflow_instance(instance, activated_by=request.user)
            except ValueError as e:
                return Response(
                    {"detail": f"Draft created but activation failed: {e}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response(
            WorkflowInstanceSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


    @action(detail=False, methods=["post"], url_path="from-campaign")
    def from_campaign(self, request):
        """
        POST /instances/from-campaign/

        Body:
            campaign_id  — PK of the Campaign to attach
            assignments  — dict { step_id: user_id } for manual overrides (optional)
            activate     — bool, whether to also activate after creation (default False)

        Resolution path:
            module = "campaign"
            scope_node = campaign.scope_node
            via resolve_workflow_template_version() — walk-up, gated on activation

        Returns the created draft instance with full group/step detail.
        """
        campaign_id = request.data.get("campaign_id")
        assignments = request.data.get("assignments", {})
        activate_after = request.data.get("activate", False)

        if not campaign_id:
            return Response(
                {"detail": "campaign_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.campaigns.models import Campaign, CampaignStatus
        try:
            campaign = Campaign.objects.select_related("scope_node").get(pk=campaign_id)
        except Campaign.DoesNotExist:
            return Response({"detail": "Campaign not found."}, status=status.HTTP_404_NOT_FOUND)

        if campaign.status != CampaignStatus.PENDING_WORKFLOW:
            return Response(
                {
                    "detail": (
                        f"Campaign is in status '{campaign.status}', "
                        "expected 'pending_workflow'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.access.models import PermissionAction, PermissionResource
        from apps.access.services import user_has_permission_including_ancestors
        can_start = (
            campaign.created_by_id == request.user.pk
            or user_has_permission_including_ancestors(
                request.user,
                PermissionAction.START_WORKFLOW,
                PermissionResource.CAMPAIGN,
                campaign.scope_node,
            )
        )
        if not can_start:
            return Response(
                {"detail": "You do not have permission to start a workflow for this campaign."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            template_version = resolve_workflow_template_version(
                module="campaign",
                scope_node=campaign.scope_node,
            )
        except ModuleInactiveError as e:
            return Response({"detail": str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except WorkflowNotConfiguredError as e:
            return Response({"detail": str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        instance = create_workflow_instance_draft(
            template_version=template_version,
            subject_type="campaign",
            subject_id=campaign.pk,
            subject_scope_node=campaign.scope_node,
            started_by=request.user,
        )

        if assignments:
            try:
                apply_step_assignment_overrides(instance, assignments, campaign.scope_node)
            except ValueError as e:
                return Response(
                    {"detail": f"Assignment override failed: {e}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if activate_after:
            try:
                instance = activate_workflow_instance(instance, activated_by=request.user)
            except ValueError as e:
                return Response(
                    {"detail": f"Draft created but activation failed: {e}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response(
            WorkflowInstanceSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


class WorkflowInstanceGroupViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowInstanceGroupSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowInstanceGroup.objects.select_related("instance", "step_group").prefetch_related(
            "instance_steps"
        ).filter(instance__subject_scope_node_id__in=visible_scope_ids)
        instance_id = self.request.query_params.get("instance")
        if instance_id:
            qs = qs.filter(instance_id=instance_id)
        return qs.order_by("display_order")


class WorkflowInstanceStepViewSet(ModelViewSet):
    """
    WorkflowInstanceStep endpoints.

    Actions:
        POST   /instance-steps/{id}/approve/   — approve this step (assigned user only)
        POST   /instance-steps/{id}/reject/   — reject this step (assigned user only)
        POST   /instance-steps/{id}/reassign/ — reassign step (requires reassign permission)
    """
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowInstanceStepSerializer

    def get_queryset(self):
        # Visibility = subtree: eligible users always have a role assignment in the scope tree,
        # so visible_scope_ids will include the instance's subject_scope_node.
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowInstanceStep.objects.select_related(
            "instance_group", "workflow_step", "assigned_user"
        ).filter(instance_group__instance__subject_scope_node_id__in=visible_scope_ids)
        group_id = self.request.query_params.get("instance_group")
        if group_id:
            qs = qs.filter(instance_group_id=group_id)
        return qs

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """POST /instance-steps/{id}/approve/ — approve this step. Actor must be the assigned user."""
        instance_step = self.get_object()
        note = request.data.get("note", "")
        try:
            instance_step = approve_workflow_step(instance_step, acted_by=request.user, note=note)
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowInstanceStepSerializer(instance_step).data)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """POST /instance-steps/{id}/reject/ — reject this step. Actor must be the assigned user."""
        instance_step = self.get_object()
        note = request.data.get("note", "")
        try:
            instance_step = reject_workflow_step(instance_step, acted_by=request.user, note=note)
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowInstanceStepSerializer(instance_step).data)

    @action(detail=True, methods=["post"], url_path="reassign")
    def reassign(self, request, pk=None):
        """POST /instance-steps/{id}/reassign/ — reassign to new user. Requires reassign permission."""
        instance_step = self.get_object()
        new_user_id = request.data.get("user_id")
        note = request.data.get("note", "")
        if not new_user_id:
            return Response({"detail": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            new_user = User.objects.get(pk=new_user_id)
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            instance_step = reassign_workflow_step(
                instance_step, new_user=new_user, reassigned_by=request.user, note=note
            )
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowInstanceStepSerializer(instance_step).data)

    @action(detail=True, methods=["get"], url_path="split-options")
    def split_options(self, request, pk=None):
        """GET /instance-steps/{id}/split-options/ — fetch allowed entities and approvers for a RUNTIME_SPLIT_ALLOCATION step."""
        instance_step = self.get_object()
        from apps.workflow.services_split import get_runtime_split_options
        from apps.workflow.services import StepActionError
        try:
            data = get_runtime_split_options(instance_step, request.user)
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(data)

    @action(detail=True, methods=["post"], url_path="submit-split")
    def submit_split(self, request, pk=None):
        """POST /instance-steps/{id}/submit-split/ — submit runtime invoice split allocations."""
        instance_step = self.get_object()
        allocations_payload = request.data.get("allocations", [])
        note = request.data.get("note", "")
        if not isinstance(allocations_payload, list):
            return Response({"detail": "allocations must be a list."}, status=status.HTTP_400_BAD_REQUEST)
        from apps.workflow.services_split import submit_runtime_invoice_split
        from apps.workflow.services import StepActionError
        try:
            result = submit_runtime_invoice_split(instance_step, actor=request.user, allocations_payload=allocations_payload, note=note)
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, pk=None):
        """
        POST /instance-steps/{id}/assign/
        Assign a user to a step while the instance is still DRAFT.
        Does NOT emit events or require reassign permission.
        Authority: caller must have direct assignment at instance.subject_scope_node.
        """
        instance_step = self.get_object()
        instance = instance_step.instance_group.instance

        if err := user_can_act_on_scope_response(request.user, instance.subject_scope_node_id, "assign steps on this workflow"):
            return err

        if instance.status != InstanceStatus.DRAFT:
            return Response(
                {"detail": f"Cannot assign step: instance status is '{instance.status}', expected 'DRAFT'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_id = request.data.get("user_id")
        if not user_id:
            return Response({"detail": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        User = get_user_model()
        try:
            new_user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        # Eligibility check — reuse existing service logic
        eligible = get_eligible_users_for_step(
            instance_step.workflow_step, instance.subject_scope_node
        )
        if not eligible.filter(pk=new_user.pk).exists():
            return Response(
                {"detail": f"User {new_user.email} is not eligible for this step."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.workflow.models import AssignmentState
        instance_step.assigned_user = new_user
        instance_step.assignment_state = AssignmentState.ASSIGNED
        instance_step.save(update_fields=["assigned_user", "assignment_state"])
        return Response(WorkflowInstanceStepSerializer(instance_step).data)


class MyTasksView(APIView):
    """
    GET /tasks/me/
    Returns all actionable tasks (both step tasks and branch tasks) for the current user.
    Branch tasks are included when the user is assigned to a pending branch.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Step tasks
        step_tasks = get_pending_tasks_for_user(request.user)
        step_data = [
            {
                "task_kind": "step",
                "instance_step_id": ist.id,
                "branch_id": None,
                "instance_id": ist.instance_group.instance_id,
                "subject_type": ist.instance_group.instance.subject_type,
                "subject_id": ist.instance_group.instance.subject_id,
                "subject_scope_node_id": ist.instance_group.instance.subject_scope_node_id,
                "instance_status": ist.instance_group.instance.status,
                "group_name": ist.instance_group.step_group.name,
                "group_order": ist.instance_group.display_order,
                "step_name": ist.workflow_step.name,
                "step_order": ist.workflow_step.display_order,
                "assigned_user_id": ist.assigned_user_id,
                "status": ist.status,
                "created_at": ist.created_at,
                "target_scope_node": None,
                "target_scope_node_name": None,
                "split_step_name": None,
            }
            for ist in step_tasks
        ]

        # Branch tasks — user is assigned to a PENDING branch
        branch_tasks = (
            WorkflowInstanceBranch.objects
            .filter(
                assigned_user=request.user,
                status=BranchStatus.PENDING,
                instance__status=InstanceStatus.ACTIVE,
            )
            .select_related(
                "instance",
                "parent_instance_step__workflow_step",
                "parent_instance_step__instance_group__step_group",
                "target_scope_node",
            )
            .order_by("created_at")
        )
        branch_data = [
            {
                "task_kind": "branch",
                "instance_step_id": None,
                "branch_id": b.id,
                "instance_id": b.instance_id,
                "subject_type": b.instance.subject_type,
                "subject_id": b.instance.subject_id,
                "subject_scope_node_id": b.instance.subject_scope_node_id,
                "instance_status": b.instance.status,
                "group_name": b.parent_instance_step.instance_group.step_group.name,
                "group_order": b.parent_instance_step.instance_group.display_order,
                "step_name": b.target_scope_node.name if b.target_scope_node else b.parent_instance_step.workflow_step.name,
                "step_order": b.parent_instance_step.workflow_step.display_order,
                "assigned_user_id": b.assigned_user_id,
                "status": b.status,
                "created_at": b.created_at,
                "target_scope_node": b.target_scope_node_id,
                "target_scope_node_name": b.target_scope_node.name if b.target_scope_node else None,
                "split_step_name": b.parent_instance_step.workflow_step.name,
            }
            for b in branch_tasks
        ]

        return Response(step_data + branch_data)


class TaskReviewView(APIView):
    """
    GET /api/v1/workflow/tasks/{task_kind}/{id}/review/

    Returns rich context for the approval review drawer.
    task_kind: "step" | "branch"
    id: instance_step_id or branch_id
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, task_kind, pk):
        if task_kind == "step":
            return self._step_review(request, pk)
        if task_kind == "branch":
            return self._branch_review(request, pk)
        return Response(
            {"detail": "Invalid task_kind. Use 'step' or 'branch'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _user_dict(u):
        if not u:
            return None
        return {
            "id": u.id,
            "email": u.email,
            "first_name": u.first_name,
            "last_name": u.last_name,
        }

    def _build_workflow_context(self, instance, current_step_id=None, current_branch_id=None):
        """Return workflow template info + all groups/steps with status."""
        tv = instance.template_version
        template = tv.template

        groups_data = []
        for ig in (
            instance.instance_groups
            .select_related("step_group")
            .prefetch_related(
                "instance_steps__workflow_step",
                "instance_steps__assigned_user",
                "instance_steps__branches__target_scope_node",
                "instance_steps__branches__assigned_user",
            )
            .order_by("display_order")
        ):
            steps_data = []
            for ist in ig.instance_steps.order_by("workflow_step__display_order"):
                branches_data = []
                for br in ist.branches.order_by("branch_index"):
                    branches_data.append({
                        "id": br.id,
                        "target_scope_node_name": br.target_scope_node.name if br.target_scope_node else None,
                        "status": br.status,
                        "assigned_user": self._user_dict(br.assigned_user),
                        "acted_at": br.acted_at,
                        "note": br.note or None,
                        "is_current_branch": br.id == current_branch_id,
                    })
                steps_data.append({
                    "id": ist.id,
                    "step_name": ist.workflow_step.name,
                    "step_kind": ist.workflow_step.step_kind,
                    "status": ist.status,
                    "assigned_user": self._user_dict(ist.assigned_user),
                    "acted_at": ist.acted_at,
                    "note": ist.note or None,
                    "is_current_step": ist.id == current_step_id,
                    "branches": branches_data,
                })
            groups_data.append({
                "id": ig.id,
                "name": ig.step_group.name,
                "display_order": ig.display_order,
                "status": ig.status,
                "steps": steps_data,
            })

        current_group_name = None
        if instance.current_group_id:
            current_group_name = instance.current_group.step_group.name

        return {
            "template_name": template.name,
            "template_version_number": tv.version_number,
            "instance_id": instance.id,
            "instance_status": instance.status,
            "started_at": instance.started_at,
            "completed_at": instance.completed_at,
            "current_group_name": current_group_name,
            "groups": groups_data,
        }

    def _build_timeline(self, instance):
        events = (
            instance.events
            .select_related("actor_user", "target_user")
            .order_by("created_at")
        )
        result = []
        for ev in events:
            result.append({
                "id": ev.id,
                "event_type": ev.event_type,
                "actor": self._user_dict(ev.actor_user),
                "target": self._user_dict(ev.target_user),
                "metadata": ev.metadata,
                "created_at": ev.created_at,
            })
        return result

    def _build_invoice_subject(self, instance):
        """Fetch and shape invoice + vendor + documents for the review payload."""
        from apps.invoices.models import Invoice, InvoiceDocument
        from apps.vendors.models import Vendor

        try:
            invoice = (
                Invoice.objects
                .select_related("scope_node", "created_by", "vendor", "vendor__onboarding_submission")
                .prefetch_related("documents__submission")
                .get(pk=instance.subject_id)
            )
        except Invoice.DoesNotExist:
            return {"type": "invoice", "invoice": None, "vendor": None, "documents": [], "missing": True}

        # Invoice summary
        invoice_data = {
            "id": invoice.id,
            "title": invoice.title,
            "vendor_invoice_number": invoice.vendor_invoice_number or None,
            "amount": str(invoice.amount),
            "currency": invoice.currency,
            "po_number": invoice.po_number or None,
            "invoice_date": invoice.invoice_date,
            "due_date": invoice.due_date,
            "status": invoice.status,
            "description": invoice.description or None,
            "scope_node_id": invoice.scope_node_id,
            "scope_node_name": invoice.scope_node.name,
            "submitted_by": self._user_dict(invoice.created_by),
            "created_at": invoice.created_at,
        }

        # Vendor context
        vendor_data = None
        if invoice.vendor_id:
            v = invoice.vendor
            gstin = None
            pan = None
            if v.onboarding_submission_id:
                sub = v.onboarding_submission
                gstin = sub.normalized_gstin or None
                pan = sub.normalized_pan or None
            vendor_data = {
                "id": v.id,
                "vendor_name": v.vendor_name,
                "email": v.email or None,
                "phone": v.phone or None,
                "sap_vendor_id": v.sap_vendor_id,
                "po_mandate_enabled": v.po_mandate_enabled,
                "operational_status": v.operational_status,
                "marketing_status": v.marketing_status,
                "gstin": gstin,
                "pan": pan,
            }

        # Documents
        docs = []
        for doc in invoice.documents.order_by("-created_at"):
            docs.append({
                "id": doc.id,
                "document_type": doc.document_type,
                "file_name": doc.file_name,
                "file_type": doc.file_type,
                "has_file": bool(doc.file),
            })

        return {
            "type": "invoice",
            "invoice": invoice_data,
            "vendor": vendor_data,
            "documents": docs,
            "missing": False,
        }

    def _build_subject(self, instance):
        if instance.subject_type == "invoice":
            return self._build_invoice_subject(instance)
        return {"type": instance.subject_type, "missing": False}

    def _build_allocation_context(self, instance_step):
        """Return existing allocations + step config for a RUNTIME_SPLIT_ALLOCATION step."""
        from apps.invoices.models import InvoiceAllocation
        allocations = (
            InvoiceAllocation.objects
            .filter(split_step=instance_step)
            .select_related("entity", "category", "subcategory", "campaign", "budget", "selected_approver")
            .order_by("id")
        )
        step = instance_step.workflow_step
        return {
            "is_runtime_split": True,
            "step_config": {
                "allocation_total_policy": step.allocation_total_policy,
                "require_category": step.require_category,
                "require_subcategory": step.require_subcategory,
                "require_budget": step.require_budget,
                "require_campaign": step.require_campaign,
                "allow_multiple_lines_per_entity": step.allow_multiple_lines_per_entity,
                "approver_selection_mode": step.approver_selection_mode,
            },
            "allocations": [
                {
                    "id": a.id,
                    "entity_id": a.entity_id,
                    "entity_name": a.entity.name if a.entity else None,
                    "amount": str(a.amount),
                    "percentage": str(a.percentage) if a.percentage else None,
                    "category_id": a.category_id,
                    "category_name": a.category.name if a.category else None,
                    "subcategory_id": a.subcategory_id,
                    "subcategory_name": a.subcategory.name if a.subcategory else None,
                    "campaign_id": a.campaign_id,
                    "campaign_name": a.campaign.name if a.campaign else None,
                    "budget_id": a.budget_id,
                    "selected_approver": self._user_dict(a.selected_approver),
                    "status": a.status,
                    "rejection_reason": a.rejection_reason,
                    "note": a.note,
                    "branch_id": a.branch_id,
                    "revision_number": a.revision_number,
                }
                for a in allocations
            ],
        }

    # ── Step review ──────────────────────────────────────────────────────────

    def _step_review(self, request, pk):
        from apps.access.selectors import get_user_visible_scope_ids
        visible = get_user_visible_scope_ids(request.user)

        try:
            ist = (
                WorkflowInstanceStep.objects
                .select_related(
                    "instance_group__instance__template_version__template",
                    "instance_group__instance__subject_scope_node",
                    "instance_group__instance__current_group__step_group",
                    "instance_group__step_group",
                    "workflow_step",
                    "assigned_user",
                    "reassigned_from_user",
                    "reassigned_by",
                )
                .get(pk=pk)
            )
        except WorkflowInstanceStep.DoesNotExist:
            return Response({"detail": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        instance = ist.instance_group.instance
        if instance.subject_scope_node_id not in visible and ist.assigned_user_id != request.user.id:
            return Response({"detail": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        task_data = {
            "task_kind": "step",
            "instance_step_id": ist.id,
            "branch_id": None,
            "step_name": ist.workflow_step.name,
            "step_kind": ist.workflow_step.step_kind,
            "group_name": ist.instance_group.step_group.name,
            "status": ist.status,
            "assigned_user": self._user_dict(ist.assigned_user),
            "reassigned_from_user": self._user_dict(ist.reassigned_from_user),
            "reassigned_by": self._user_dict(ist.reassigned_by),
            "reassigned_at": ist.reassigned_at,
            "created_at": ist.created_at,
        }

        # Include split allocation context for RUNTIME_SPLIT_ALLOCATION steps
        allocation_context = None
        if ist.workflow_step.step_kind == StepKind.RUNTIME_SPLIT_ALLOCATION:
            allocation_context = self._build_allocation_context(ist)

        return Response({
            "task": task_data,
            "subject": self._build_subject(instance),
            "workflow": self._build_workflow_context(instance, current_step_id=ist.id),
            "timeline": self._build_timeline(instance),
            "allocation_context": allocation_context,
        })

    # ── Branch review ────────────────────────────────────────────────────────

    def _branch_review(self, request, pk):
        from apps.access.selectors import get_user_visible_scope_ids
        visible = get_user_visible_scope_ids(request.user)

        try:
            branch = (
                WorkflowInstanceBranch.objects
                .select_related(
                    "instance__template_version__template",
                    "instance__subject_scope_node",
                    "instance__current_group__step_group",
                    "parent_instance_step__workflow_step",
                    "parent_instance_step__instance_group__step_group",
                    "target_scope_node",
                    "assigned_user",
                    "reassigned_from_user",
                    "reassigned_by",
                )
                .get(pk=pk)
            )
        except WorkflowInstanceBranch.DoesNotExist:
            return Response({"detail": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        instance = branch.instance
        if instance.subject_scope_node_id not in visible and branch.assigned_user_id != request.user.id:
            return Response({"detail": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        task_data = {
            "task_kind": "branch",
            "instance_step_id": None,
            "branch_id": branch.id,
            "step_name": branch.target_scope_node.name if branch.target_scope_node else branch.parent_instance_step.workflow_step.name,
            "split_step_name": branch.parent_instance_step.workflow_step.name,
            "group_name": branch.parent_instance_step.instance_group.step_group.name,
            "target_scope_node_id": branch.target_scope_node_id,
            "target_scope_node_name": branch.target_scope_node.name if branch.target_scope_node else None,
            "status": branch.status,
            "assigned_user": self._user_dict(branch.assigned_user),
            "reassigned_from_user": self._user_dict(branch.reassigned_from_user),
            "reassigned_by": self._user_dict(branch.reassigned_by),
            "reassigned_at": branch.reassigned_at,
            "created_at": branch.created_at,
        }

        # Include allocation info if this branch is from a RUNTIME_SPLIT_ALLOCATION step
        branch_allocation = None
        if branch.parent_instance_step.workflow_step.step_kind == StepKind.RUNTIME_SPLIT_ALLOCATION:
            try:
                from apps.invoices.models import InvoiceAllocation
                alloc = branch.invoice_allocation
                branch_allocation = {
                    "id": alloc.id,
                    "entity_id": alloc.entity_id,
                    "entity_name": alloc.entity.name if alloc.entity else None,
                    "amount": str(alloc.amount),
                    "percentage": str(alloc.percentage) if alloc.percentage else None,
                    "category_id": alloc.category_id,
                    "category_name": alloc.category.name if alloc.category else None,
                    "subcategory_id": alloc.subcategory_id,
                    "subcategory_name": alloc.subcategory.name if alloc.subcategory else None,
                    "campaign_id": alloc.campaign_id,
                    "campaign_name": alloc.campaign.name if alloc.campaign else None,
                    "budget_id": alloc.budget_id,
                    "status": alloc.status,
                    "rejection_reason": alloc.rejection_reason,
                    "note": alloc.note,
                    "revision_number": alloc.revision_number,
                }
            except Exception:
                branch_allocation = None

        return Response({
            "task": task_data,
            "subject": self._build_subject(instance),
            "workflow": self._build_workflow_context(
                instance,
                current_step_id=branch.parent_instance_step_id,
                current_branch_id=branch.id,
            ),
            "timeline": self._build_timeline(instance),
            "branch_allocation": branch_allocation,
        })


class WorkflowInstanceBranchViewSet(ModelViewSet):
    """
    WorkflowInstanceBranch endpoints.

    Actions:
        POST   /branches/{id}/approve/  — approve this branch (assigned user only)
        POST   /branches/{id}/reject/    — reject this branch (assigned user only)
        POST   /branches/{id}/reassign/ — reassign branch (requires reassign permission)
    """
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowInstanceBranchSerializer

    def get_queryset(self):
        visible_scope_ids = get_user_visible_scope_ids(self.request.user)
        qs = WorkflowInstanceBranch.objects.select_related(
            "parent_instance_step", "instance", "target_scope_node", "assigned_user"
        ).filter(
            Q(instance__subject_scope_node_id__in=visible_scope_ids)
            | Q(assigned_user=self.request.user)
        )
        instance_id = self.request.query_params.get("instance")
        if instance_id:
            qs = qs.filter(instance_id=instance_id)
        parent_step_id = self.request.query_params.get("parent_instance_step")
        if parent_step_id:
            qs = qs.filter(parent_instance_step_id=parent_step_id)
        return qs

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """POST /branches/{id}/approve/ — approve this branch. Actor must be the assigned user."""
        branch = self.get_object()
        note = request.data.get("note", "")
        try:
            branch = approve_workflow_branch(branch, acted_by=request.user, note=note)
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowInstanceBranchSerializer(branch).data)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """POST /branches/{id}/reject/ — reject this branch. Actor must be the assigned user."""
        branch = self.get_object()
        note = request.data.get("note", "")
        try:
            branch = reject_workflow_branch(branch, acted_by=request.user, note=note)
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowInstanceBranchSerializer(branch).data)

    @action(detail=True, methods=["post"], url_path="reassign")
    def reassign(self, request, pk=None):
        """POST /branches/{id}/reassign/ — reassign branch to new user. Requires reassign permission."""
        branch = self.get_object()
        new_user_id = request.data.get("user_id")
        note = request.data.get("note", "")
        if not new_user_id:
            return Response({"detail": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        User = get_user_model()
        try:
            new_user = User.objects.get(pk=new_user_id)
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            branch = reassign_workflow_branch(
                branch, new_user=new_user, reassigned_by=request.user, note=note
            )
        except StepActionError as e:
            return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WorkflowInstanceBranchSerializer(branch).data)
