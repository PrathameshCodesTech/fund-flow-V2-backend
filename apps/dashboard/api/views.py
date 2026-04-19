"""
Dashboard API views — ops dashboard, insights, and invoice control tower.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.dashboard.services import (
    get_ops_dashboard_payload,
    get_invoice_control_tower_payload,
    get_insights_payload,
)
from apps.invoices.selectors import user_can_access_invoice


class OpsDashboardView(APIView):
    """
    GET /api/v1/dashboard/ops/

    Returns the full ops dashboard payload:
    - KPIs
    - Attention queues
    - My pending tasks
    - Recent activity
    - Lifecycle summaries
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        payload = get_ops_dashboard_payload(request.user)
        return Response(payload)


class InsightsView(APIView):
    """
    GET /api/v1/dashboard/insights/

    Returns all insights/analytics data in one call.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        payload = get_insights_payload(request.user)
        return Response(payload)
