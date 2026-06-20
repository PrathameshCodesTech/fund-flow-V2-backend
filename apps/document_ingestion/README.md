# External Document Ingestion

Provider-neutral ingestion for invoice and payment files received from external
storage. The app owns transport, extraction, matching recommendations, review
state, retries, and audit history. It does not own vendors, invoices, or
payments; validated actions call the existing domain services.

## Current capabilities

- Local-folder connector for development and cron-based deployments
- Connector contract ready for SFTP, FTP, and SharePoint adapters
- SHA-256 duplicate detection
- PDF text extraction with OCR fallback for scanned PDFs
- Excel, CSV, JSON, and text extraction
- Multiple business records per physical file
- Vendor matching by SAP vendor code, GSTIN, or email
- Invoice matching by invoice number or SAP finance reference
- Amount and currency conflict validation
- Finance/admin-only review API
- Explicit, permission-checked payment application
- Immutable processing event history
- File-size and PDF-page safety limits

## Processing boundary

```text
connector -> physical document -> extracted records -> deterministic match
          -> finance review -> existing invoice payment service
```

Extraction never marks an invoice paid. Payment application requires:

- record classified as `payment_advice`
- one validated invoice match
- matching amount and currency
- payment date and UTR
- actor authorized by the existing invoice payment service

## API

Base path: `/api/v1/document-ingestion/`

```text
GET  sources/
GET  documents/
GET  documents/counts/
POST documents/upload/
GET  documents/{id}/
GET  documents/{id}/download/
POST documents/{id}/process/
POST documents/{id}/match/
POST documents/{id}/quarantine/

GET  records/
GET  records/{id}/
POST records/{id}/correct/
POST records/{id}/link-invoice/
POST records/{id}/apply-payment/
```

These endpoints require a finance, organization-admin, tenant-admin, or
superuser account.

The source file is not exposed as a raw media URL. Frontends must use the
authenticated `download_url` returned by document detail and fetch it as a blob
with the JWT API client; a plain anchor will not include the auth header. Production web-server
configuration must not publicly serve `external_document_imports/` from the
media directory.

## Local connector

Create an `ExternalDocumentSource` using Django admin:

```text
connector_type: local
base_path: /opt/vims/document-ingestion
public_config:
  {"inbox":"inbox","archive":"archive","quarantine":"quarantine"}
```

Then poll it manually or from cron:

```bash
python manage.py poll_external_documents --source 1
```

Use `--no-archive` during non-destructive local testing.

## Environment settings

```env
DOCUMENT_INGESTION_MAX_FILE_SIZE_MB=25
DOCUMENT_INGESTION_MAX_PDF_PAGES=50
```

Connector secrets must use environment variables or a secrets manager. The
database source model stores only a `config_key` prefix and non-secret paths.

## SFTP connector

The SFTP adapter resolves credentials from environment variables using the
source `config_key`; credentials are never stored in `ExternalDocumentSource`.

For a source with `config_key: VIMS_UAT_SFTP`:

```env
VIMS_UAT_SFTP_HOST=sftp.example.com
VIMS_UAT_SFTP_PORT=22
VIMS_UAT_SFTP_USERNAME=vims_ingestion
VIMS_UAT_SFTP_PASSWORD=<store-in-server-secret>
# Or use key authentication instead of a password:
# VIMS_UAT_SFTP_PRIVATE_KEY_PATH=/etc/vims/secrets/vims-ingestion.key
# VIMS_UAT_SFTP_PRIVATE_KEY_PASSWORD=<optional-key-passphrase>
VIMS_UAT_SFTP_KNOWN_HOSTS_PATH=/etc/vims/known_hosts
VIMS_UAT_SFTP_TIMEOUT_SECONDS=30
```

The known-hosts file is mandatory. The connector rejects unknown SSH host keys.

Example source configuration:

```text
connector_type: sftp
config_key: VIMS_UAT_SFTP
base_path: /
public_config:
  {"inbox":"inbox","archive":"archive","quarantine":"quarantine"}
```

Poll it manually or from cron:

```bash
python manage.py poll_external_documents --source 1
```

## Remaining client adapters

FTP and SharePoint adapters are still intentionally unimplemented. After the
client confirms a different provider, add only that provider's adapter under
`connectors/` and register it in `connectors/__init__.py`; the ingestion
services and APIs remain unchanged.

Before enabling unattended polling, confirm:

- SFTP, FTP, SharePoint, or another source
- source, archive, and quarantine folder behavior
- authoritative vendor and invoice identifiers
- real invoice and payment file samples
- polling interval and expected volume
- retention and alerting requirements
- whether automatic payment application is permitted or finance confirmation is mandatory
