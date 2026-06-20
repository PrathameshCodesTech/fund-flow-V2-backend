import shutil
from datetime import datetime, timezone
from pathlib import Path

from apps.document_ingestion.connectors.base import ConnectorError, RemoteDocument


class LocalFolderConnector:
    """Development and cron-friendly filesystem connector.

    The configured root is authoritative; discovered and destination paths are
    resolved and checked so traversal cannot escape that root.
    """

    def __init__(self, source):
        self.source = source
        root = source.base_path or source.public_config.get("root_path", "")
        if not root:
            raise ConnectorError("Local connector requires base_path or public_config.root_path.")
        self.root = Path(root).expanduser().resolve()
        self.inbox = self._inside_root(source.public_config.get("inbox", "inbox"))
        self.archive_dir = self._inside_root(source.public_config.get("archive", "archive"))
        self.quarantine_dir = self._inside_root(source.public_config.get("quarantine", "quarantine"))

    def _inside_root(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ConnectorError("Connector path escapes the configured root.") from exc
        return candidate

    def list_documents(self) -> list[RemoteDocument]:
        if not self.inbox.exists():
            return []
        result = []
        for path in sorted(self.inbox.iterdir()):
            if not path.is_file():
                continue
            stat = path.stat()
            result.append(
                RemoteDocument(
                    identifier=str(path.relative_to(self.root)).replace("\\", "/"),
                    path=str(path),
                    filename=path.name,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return result

    def open_document(self, document: RemoteDocument):
        path = Path(document.path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ConnectorError("Document path escapes the configured root.") from exc
        return path.open("rb")

    def archive(self, document: RemoteDocument) -> None:
        self._move(document, self.archive_dir)

    def quarantine(self, document: RemoteDocument, reason: str) -> None:
        self._move(document, self.quarantine_dir)

    def _move(self, document: RemoteDocument, destination: Path) -> None:
        source_path = Path(document.path).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        target = (destination / document.filename).resolve()
        try:
            source_path.relative_to(self.root)
            target.relative_to(self.root)
        except ValueError as exc:
            raise ConnectorError("Move target escapes the configured root.") from exc
        if target.exists():
            target = destination / f"{source_path.stem}-{int(source_path.stat().st_mtime)}{source_path.suffix}"
        shutil.move(str(source_path), str(target))

