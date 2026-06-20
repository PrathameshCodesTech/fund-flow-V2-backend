from apps.document_ingestion.connectors.base import ConnectorError, RemoteDocument
from apps.document_ingestion.connectors.local import LocalFolderConnector
from apps.document_ingestion.connectors.sftp import SFTPConnector


def build_connector(source):
    if source.connector_type == "local":
        return LocalFolderConnector(source)
    if source.connector_type == "sftp":
        return SFTPConnector(source)
    raise ConnectorError(
        f"Connector '{source.connector_type}' is not installed. "
        "Add its adapter without changing the ingestion pipeline."
    )


__all__ = ["ConnectorError", "RemoteDocument", "LocalFolderConnector", "SFTPConnector", "build_connector"]
