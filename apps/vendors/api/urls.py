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
    PublicInvitationSubmissionView,
    PublicInvitationSubmitExcelView,
    PublicInvitationSubmitManualView,
    PublicInvitationView,
    PublicVendorActivateSetPasswordView,
    PublicVendorActivateValidateView,
    VendorAttachmentViewSet,
    VendorInvitationViewSet,
    VendorPortalProfileRevisionView,
    VendorPortalProfileView,
    VendorPortalRevisionHistoryView,
    VendorPortalSaveDraftRevisionView,
    VendorPortalSubmitRevisionView,
    VendorProfileRevisionViewSet,
    VendorSendToOptionsView,
    VendorSubmissionRouteViewSet,
    VendorSubmissionViewSet,
    VendorViewSet,
)

router = DefaultRouter()
router.register("invitations", VendorInvitationViewSet, basename="vendorinvitation")
router.register("submissions", VendorSubmissionViewSet, basename="vendorsubmission")
router.register("attachments", VendorAttachmentViewSet, basename="vendorattachment")
router.register("send-to-options", VendorSubmissionRouteViewSet, basename="vendorsubmissionroute")
router.register("", VendorViewSet, basename="vendor")

# Nested router: /api/v1/vendors/{vendor_pk}/profile-revisions/
revision_router = DefaultRouter()
revision_router.register("profile-revisions", VendorProfileRevisionViewSet, basename="vendorprofilerevision")

urlpatterns = [
    # Authenticated portal endpoints must come before the catch-all VendorViewSet
    # router, otherwise "my-vendor" is treated as a vendor primary key.
    path(
        "my-vendor/",
        MyVendorView.as_view(),
        name="my-vendor",
    ),
    # Vendor-facing send-to options (minimal payload, no template internals)
    path(
        "vendor-send-to-options/",
        VendorSendToOptionsView.as_view(),
        name="vendor-send-to-options",
    ),

    # Vendor portal profile revision endpoints
    path("portal/profile/", VendorPortalProfileView.as_view(), name="portal-profile"),
    path("portal/profile/revision/", VendorPortalProfileRevisionView.as_view(), name="portal-profile-revision"),
    path("portal/profile/revision/save-draft/", VendorPortalSaveDraftRevisionView.as_view(), name="portal-profile-revision-save-draft"),
    path("portal/profile/revision/submit/", VendorPortalSubmitRevisionView.as_view(), name="portal-profile-revision-submit"),
    path("portal/profile/revisions/", VendorPortalRevisionHistoryView.as_view(), name="portal-profile-revision-history"),

    # Internal vendor profile revision management (nested under vendor PK)
    path("<int:vendor_pk>/", include(revision_router.urls)),

    path("", include(router.urls)),

    # Public invitation flow
    path(
        "public/invitations/<str:token>/",
        PublicInvitationView.as_view(),
        name="public-invitation-detail",
    ),
    path(
        "public/invitations/<str:token>/submission/",
        PublicInvitationSubmissionView.as_view(),
        name="public-invitation-submission",
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
