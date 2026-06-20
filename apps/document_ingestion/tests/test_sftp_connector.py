import stat
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.document_ingestion.connectors.base import ConnectorError
from apps.document_ingestion.connectors.sftp import SFTPConnector


def make_source(*, base_path="/", public_config=None):
    return SimpleNamespace(
        name="UAT SFTP",
        config_key="TEST_SFTP",
        base_path=base_path,
        public_config=public_config or {"inbox": "inbox", "archive": "archive", "quarantine": "quarantine"},
    )


class SFTPConnectorTests(SimpleTestCase):
    def setUp(self):
        self.settings = {
            "TEST_SFTP_HOST": "sftp.example.test",
            "TEST_SFTP_USERNAME": "vims_test",
            "TEST_SFTP_PASSWORD": "test-password",
            "TEST_SFTP_KNOWN_HOSTS_PATH": "/tmp/test-known-hosts",
        }

    def connector(self, **kwargs):
        with patch.dict("os.environ", self.settings, clear=False):
            return SFTPConnector(make_source(**kwargs))

    def test_rejects_paths_outside_non_root_base_path(self):
        with self.assertRaisesMessage(ConnectorError, "escapes the configured base_path"):
            self.connector(
                base_path="/finance",
                public_config={"inbox": "../../outside", "archive": "archive", "quarantine": "quarantine"},
            )

    def test_lists_regular_files_in_inbox_only(self):
        connector = self.connector()
        remote_file = SimpleNamespace(
            filename="payment.txt",
            st_mode=stat.S_IFREG | 0o644,
            st_size=42,
            st_mtime=time.time(),
        )
        remote_directory = SimpleNamespace(
            filename="nested",
            st_mode=stat.S_IFDIR | 0o755,
            st_size=0,
            st_mtime=time.time(),
        )
        sftp = Mock()
        sftp.listdir_attr.return_value = [remote_directory, remote_file]
        client = Mock()

        with patch.object(connector, "_connect", return_value=(client, sftp)):
            documents = connector.list_documents()

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].identifier, "/inbox/payment.txt")
        self.assertEqual(documents[0].filename, "payment.txt")
        self.assertEqual(documents[0].size, 42)
        sftp.close.assert_called_once()
        client.close.assert_called_once()

    def test_archive_moves_to_archive_folder_without_overwriting(self):
        connector = self.connector()
        sftp = Mock()
        sftp.stat.side_effect = OSError("missing")
        client = Mock()
        document = SimpleNamespace(path="/inbox/payment.txt", filename="payment.txt")

        with patch.object(connector, "_connect", return_value=(client, sftp)):
            connector.archive(document)

        sftp.rename.assert_called_once_with("/inbox/payment.txt", "/archive/payment.txt")
