from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.vendors.api.views import (
    MyVendorView,
    PublicFinanceActionView,
    PublicFinanceApproveView,
    PublicFinanceDownloadAttachmentView,
    PublicFinanceDownloadExportView,
    PublicFinanceDownloadSourceView,
    PublicFinanceRejectView,
    PublicInvitationAttachView,
    PublicInvitationFinalizeView,
    PublicInvitationSubmitExcelView,
    PublicInvitationSubmitManualView,
    PublicInvitationView,
    PublicVendorActivateSetPasswordView,
    PublicVendorActivateValidateView,
    VendorAttachmentViewSet,
    VendorInvitationViewSet,
    VendorSubmissionViewSet,
    VendorViewSet,
)

router = DefaultRouter()
router.register("invitations", VendorInvitationViewSet, basename="vendorinvitation")
router.register("submissions", VendorSubmissionViewSet, basename="vendorsubmission")
router.register("attachments", VendorAttachmentViewSet, basename="vendorattachment")
router.register("", VendorViewSet, basename="vendor")

urlpatterns = [
    # Authenticated portal endpoints must come before the catch-all VendorViewSet
    # router, otherwise "my-vendor" is treated as a vendor primary key.
    path(
        "my-vendor/",
        MyVendorView.as_view(),
        name="my-vendor",
    ),

    path("", include(router.urls)),

    # Public invitation flow
    path(
        "public/invitations/<str:token>/",
        PublicInvitationView.as_view(),
        name="public-invitation-detail",
    ),
    path(
        "public/invitations/<str:token>/submit-manual/",
        PublicInvitationSubmitManualView.as_view(),
        name="public-invitation-submit-manual",
    ),
    path(
        "public/invitations/<str:token>/submit-excel/",
        PublicInvitationSubmitExcelView.as_view(),
        name="public-invitation-submit-excel",
    ),
    path(
        "public/invitations/<str:token>/attachments/",
        PublicInvitationAttachView.as_view(),
        name="public-invitation-attachments",
    ),
    path(
        "public/invitations/<str:token>/finalize/",
        PublicInvitationFinalizeView.as_view(),
        name="public-invitation-finalize",
    ),

    # Public finance action flow
    path(
        "public/finance/<str:token>/",
        PublicFinanceActionView.as_view(),
        name="public-finance-action",
    ),
    path(
        "public/finance/<str:token>/approve/",
        PublicFinanceApproveView.as_view(),
        name="public-finance-approve",
    ),
    path(
        "public/finance/<str:token>/reject/",
        PublicFinanceRejectView.as_view(),
        name="public-finance-reject",
    ),
    # Finance document downloads (token-gated, no raw paths)
    path(
        "public/finance/<str:token>/download/export-excel/",
        PublicFinanceDownloadExportView.as_view(),
        name="public-finance-download-export",
    ),
    path(
        "public/finance/<str:token>/download/source-excel/",
        PublicFinanceDownloadSourceView.as_view(),
        name="public-finance-download-source",
    ),
    path(
        "public/finance/<str:token>/download/attachment/<int:attachment_id>/",
        PublicFinanceDownloadAttachmentView.as_view(),
        name="public-finance-download-attachment",
    ),

    # Vendor portal activation (public, token-gated)
    path(
        "public/activate/<str:uid>/<str:token>/",
        PublicVendorActivateValidateView.as_view(),
        name="public-vendor-activate-validate",
    ),
    path(
        "public/activate/<str:uid>/<str:token>/set-password/",
        PublicVendorActivateSetPasswordView.as_view(),
        name="public-vendor-activate-set-password",
    ),

]
