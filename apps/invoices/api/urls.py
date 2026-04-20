from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.invoices.api.views import InvoiceViewSet, VendorInvoiceSubmissionViewSet, InvoiceDocumentViewSet

invoice_allocations = InvoiceViewSet.as_view({
    "get": "allocations",
})

submission_router = DefaultRouter()
submission_router.register("vendor-invoice-submissions", VendorInvoiceSubmissionViewSet, basename="vendor-invoice-submission")
submission_router.register("invoice-documents", InvoiceDocumentViewSet, basename="invoice-document")

invoice_list = InvoiceViewSet.as_view({
    "get": "list",
    "post": "create",
})
invoice_detail = InvoiceViewSet.as_view({
    "get": "retrieve",
    "put": "update",
    "patch": "partial_update",
    "delete": "destroy",
})
invoice_submit = InvoiceViewSet.as_view({
    "post": "submit",
})
invoice_eligible_workflows = InvoiceViewSet.as_view({
    "get": "eligible_workflows",
})
invoice_attach_workflow = InvoiceViewSet.as_view({
    "post": "attach_workflow",
})
invoice_control_tower = InvoiceViewSet.as_view({
    "get": "control_tower",
})

urlpatterns = [
    path("", invoice_list, name="invoice-list"),
    path("", include(submission_router.urls)),
    path("<str:pk>/", invoice_detail, name="invoice-detail"),
    path("<str:pk>/submit/", invoice_submit, name="invoice-submit"),
    path("<str:pk>/eligible-workflows/", invoice_eligible_workflows, name="invoice-eligible-workflows"),
    path("<str:pk>/attach-workflow/", invoice_attach_workflow, name="invoice-attach-workflow"),
    path("<str:pk>/control-tower/", invoice_control_tower, name="invoice-control-tower"),
    path("<str:pk>/allocations/", invoice_allocations, name="invoice-allocations"),
]
