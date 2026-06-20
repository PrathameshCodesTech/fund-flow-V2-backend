"""SFTP transport adapter for external document sources.

Credentials are resolved from environment variables using the source's
``config_key``. Host keys are always verified through a known_hosts file;
the connector never accepts an unknown server key automatically.
"""

from __future__ import annotations

import os
import posixpath
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Iterator
from uuid import uuid4

import paramiko

from apps.document_ingestion.connectors.base import ConnectorError, RemoteDocument


class SFTPConnector:
    """Poll, download, archive, and quarantine files over SFTP."""

    def __init__(self, source):
        self.source = source
        self.config_key = source.config_key.strip()
        if not self.config_key:
            raise ConnectorError("SFTP connector requires a source config_key.")

        self.host = self._required_setting("HOST")
        self.username = self._required_setting("USERNAME")
        self.port = self._integer_setting("PORT", default=22)
        self.timeout = self._integer_setting("TIMEOUT_SECONDS", default=30)
        self.password = os.getenv(f"{self.config_key}_PASSWORD") or None
        self.private_key_path = os.getenv(f"{self.config_key}_PRIVATE_KEY_PATH") or None
        self.private_key_password = os.getenv(f"{self.config_key}_PRIVATE_KEY_PASSWORD") or None
        self.known_hosts_path = self._required_setting("KNOWN_HOSTS_PATH")

        if not self.password and not self.private_key_path:
            raise ConnectorError(
                f"Set {self.config_key}_PASSWORD or {self.config_key}_PRIVATE_KEY_PATH for this SFTP source."
            )

        root = source.base_path or "/"
        self.root = self._normalise_root(root)
        config = source.public_config or {}
        self.inbox = self._inside_root(config.get("inbox", "inbox"))
        self.archive_dir = self._inside_root(config.get("archive", "archive"))
        self.quarantine_dir = self._inside_root(config.get("quarantine", "quarantine"))

    def _required_setting(self, suffix: str) -> str:
        value = os.getenv(f"{self.config_key}_{suffix}", "").strip()
        if not value:
            raise ConnectorError(f"Missing required SFTP setting: {self.config_key}_{suffix}.")
        return value

    def _integer_setting(self, suffix: str, *, default: int) -> int:
        raw_value = os.getenv(f"{self.config_key}_{suffix}", str(default))
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConnectorError(f"{self.config_key}_{suffix} must be an integer.") from exc
        if value <= 0:
            raise ConnectorError(f"{self.config_key}_{suffix} must be positive.")
        return value

    @staticmethod
    def _normalise_root(value: str) -> str:
        if not value.startswith("/"):
            raise ConnectorError("SFTP base_path must be an absolute POSIX path.")
        return posixpath.normpath(value)

    def _inside_root(self, configured_path: str) -> str:
        if not configured_path:
            raise ConnectorError("SFTP source paths cannot be empty.")
        candidate = (
            posixpath.normpath(configured_path)
            if configured_path.startswith("/")
            else posixpath.normpath(posixpath.join(self.root, configured_path))
        )
        if self.root != "/" and candidate != self.root and not candidate.startswith(f"{self.root}/"):
            raise ConnectorError("SFTP source path escapes the configured base_path.")
        return candidate

    def _connect(self):
        if not os.path.isfile(self.known_hosts_path):
            raise ConnectorError(
                f"Known-hosts file does not exist: {self.known_hosts_path}. "
                "SFTP server host-key verification is required."
            )

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.load_host_keys(self.known_hosts_path)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                key_filename=self.private_key_path,
                passphrase=self.private_key_password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            return client, client.open_sftp()
        except Exception as exc:
            client.close()
            raise ConnectorError(f"Unable to connect to SFTP source '{self.source.name}': {exc}") from exc

    @contextmanager
    def _sftp_session(self) -> Iterator[paramiko.SFTPClient]:
        client, sftp = self._connect()
        try:
            yield sftp
        finally:
            try:
                sftp.close()
            finally:
                client.close()

    def list_documents(self) -> list[RemoteDocument]:
        with self._sftp_session() as sftp:
            try:
                entries = sftp.listdir_attr(self.inbox)
            except OSError as exc:
                raise ConnectorError(f"Unable to list SFTP inbox '{self.inbox}': {exc}") from exc

        documents = []
        for entry in sorted(entries, key=lambda item: item.filename):
            if not stat.S_ISREG(entry.st_mode):
                continue
            path = posixpath.join(self.inbox, entry.filename)
            documents.append(
                RemoteDocument(
                    identifier=path,
                    path=path,
                    filename=PurePosixPath(entry.filename).name,
                    size=entry.st_size,
                    modified_at=datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc),
                )
            )
        return documents

    @contextmanager
    def open_document(self, document: RemoteDocument):
        self._validate_document_path(document.path)
        with self._sftp_session() as sftp:
            try:
                with sftp.open(document.path, "rb") as file_handle:
                    yield file_handle
            except OSError as exc:
                raise ConnectorError(f"Unable to download SFTP document '{document.path}': {exc}") from exc

    def archive(self, document: RemoteDocument) -> None:
        self._move(document, self.archive_dir)

    def quarantine(self, document: RemoteDocument, reason: str) -> None:
        self._move(document, self.quarantine_dir)

    def _validate_document_path(self, path: str) -> None:
        normalised = self._inside_root(path)
        if normalised != path:
            raise ConnectorError("SFTP document path is not normalised.")
        if not path.startswith(f"{self.inbox}/"):
            raise ConnectorError("SFTP document is outside the configured inbox.")

    def _move(self, document: RemoteDocument, destination_dir: str) -> None:
        self._validate_document_path(document.path)
        with self._sftp_session() as sftp:
            self._ensure_directory(sftp, destination_dir)
            target = posixpath.join(destination_dir, document.filename)
            if self._path_exists(sftp, target):
                stem, suffix = posixpath.splitext(document.filename)
                target = posixpath.join(destination_dir, f"{stem}-{uuid4().hex[:12]}{suffix}")
            try:
                sftp.rename(document.path, target)
            except OSError as exc:
                raise ConnectorError(f"Unable to move SFTP document '{document.path}': {exc}") from exc

    @staticmethod
    def _path_exists(sftp: paramiko.SFTPClient, path: str) -> bool:
        try:
            sftp.stat(path)
        except OSError:
            return False
        return True

    @staticmethod
    def _ensure_directory(sftp: paramiko.SFTPClient, path: str) -> None:
        current = "/"
        for part in PurePosixPath(path).parts[1:]:
            current = posixpath.join(current, part)
            try:
                sftp.stat(current)
            except OSError:
                try:
                    sftp.mkdir(current)
                except OSError:
                    # A parallel poll may have created it after stat().
                    sftp.stat(current)
