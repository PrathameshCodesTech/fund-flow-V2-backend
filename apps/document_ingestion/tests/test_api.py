import tempfile

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.access.models import Role, UserRoleAssignment
from apps.core.models import NodeType, Organization, ScopeNode
from apps.document_ingestion.models import ExternalDocumentSource
from apps.document_ingestion.services import register_document
from apps.users.models import User


class DocumentIngestionApiTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media_dir.cleanup)
        self.org = Organization.objects.create(name="Horizon", code="horizon-api")
        self.scope = ScopeNode.objects.create(
            org=self.org,
            name="Marketing",
            code="marketing",
            node_type=NodeType.DEPARTMENT,
            path="/horizon-api/marketing",
            depth=0,
        )
        self.finance_role = Role.objects.create(org=self.org, name="Finance Team", code="finance_team")
        self.finance_user = User.objects.create_user("finance-api@example.com", "password")
        UserRoleAssignment.objects.create(user=self.finance_user, role=self.finance_role, scope_node=self.scope)
        self.regular_user = User.objects.create_user("regular@example.com", "password")
        self.client = APIClient()

    def test_non_finance_user_cannot_access_ingestion_queue(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.get("/api/v1/document-ingestion/documents/")
        self.assertEqual(response.status_code, 403)

    def test_finance_user_can_upload_document_for_visible_org(self):
        self.client.force_authenticate(self.finance_user)
        response = self.client.post(
            "/api/v1/document-ingestion/documents/upload/",
            {
                "org": self.org.pk,
                "file": self._file(
                    b"Payment Advice\nVendor Code: V100\nInvoice Number: INV-100\n"
                    b"Paid Amount: 100.00\nPayment Date: 20-06-2026\nUTR Number: UTR-1"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "review_required")
        self.assertEqual(len(response.data["records"]), 1)

    def test_source_api_does_not_expose_connector_paths_or_config_keys(self):
        ExternalDocumentSource.objects.create(
            org=self.org,
            name="Finance SFTP",
            connector_type="sftp",
            config_key="SECRET_PREFIX",
            base_path="/private/inbox",
            public_config={"site": "secret"},
        )
        self.client.force_authenticate(self.finance_user)
        response = self.client.get("/api/v1/document-ingestion/sources/")
        self.assertEqual(response.status_code, 200)
        item = response.data["results"][0]
        self.assertNotIn("config_key", item)
        self.assertNotIn("base_path", item)
        self.assertNotIn("public_config", item)

    def test_authenticated_download_requires_ingestion_operator(self):
        document = register_document(
            org=self.org,
            filename="private-payment.txt",
            content=b"private payment document",
            actor=self.finance_user,
        )
        self.client.force_authenticate(self.regular_user)
        denied = self.client.get(f"/api/v1/document-ingestion/documents/{document.pk}/download/")
        self.assertEqual(denied.status_code, 403)

        self.client.force_authenticate(self.finance_user)
        allowed = self.client.get(f"/api/v1/document-ingestion/documents/{document.pk}/download/")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(b"".join(allowed.streaming_content), b"private payment document")
        self.assertIn("attachment", allowed["Content-Disposition"])

    def test_counts_are_global_for_visible_queue_and_ignore_status_filter(self):
        register_document(org=self.org, filename="one.txt", content=b"first")
        register_document(org=self.org, filename="two.txt", content=b"second")
        self.client.force_authenticate(self.finance_user)
        response = self.client.get("/api/v1/document-ingestion/documents/counts/?status=failed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total"], 2)
        self.assertEqual(response.data["by_status"]["downloaded"], 2)

    @staticmethod
    def _file(content):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile("payment.txt", content, content_type="text/plain")
