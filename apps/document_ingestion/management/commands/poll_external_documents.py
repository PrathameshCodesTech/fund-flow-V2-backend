from django.core.management.base import BaseCommand, CommandError

from apps.document_ingestion.models import ExternalDocumentSource
from apps.document_ingestion.services import poll_source


class Command(BaseCommand):
    help = "Poll one or all active external document sources. Suitable for cron execution."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, help="Only poll this source ID.")
        parser.add_argument("--no-archive", action="store_true", help="Leave processed source files in place.")

    def handle(self, *args, **options):
        sources = ExternalDocumentSource.objects.filter(is_active=True).select_related("org")
        if options.get("source"):
            sources = sources.filter(pk=options["source"])
        if not sources.exists():
            raise CommandError("No active external document sources matched.")
        total = 0
        for source in sources:
            self.stdout.write(f"Polling {source.name} ({source.connector_type})...")
            result = poll_source(source, archive=not options["no_archive"])
            total += len(result.documents)
            for document in result.documents:
                self.stdout.write(f"  {document.id}: {document.original_filename} [{document.status}]")
            for error in result.errors:
                self.stderr.write(self.style.ERROR(f"  {error['identifier']}: {error['error']}"))
        self.stdout.write(self.style.SUCCESS(f"Processed {total} document(s)."))
