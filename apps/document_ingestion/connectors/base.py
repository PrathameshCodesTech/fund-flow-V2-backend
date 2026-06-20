from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, ContextManager, Protocol


class ConnectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteDocument:
    identifier: str
    path: str
    filename: str
    size: int
    modified_at: datetime | None = None


class DocumentConnector(Protocol):
    def list_documents(self) -> list[RemoteDocument]: ...

    def open_document(self, document: RemoteDocument) -> ContextManager[BinaryIO]: ...

    def archive(self, document: RemoteDocument) -> None: ...

    def quarantine(self, document: RemoteDocument, reason: str) -> None: ...
