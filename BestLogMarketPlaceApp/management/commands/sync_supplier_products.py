import logging
import logging

from django.core.management.base import BaseCommand, CommandError

from BestLogMarketPlaceApp.services.supplier_sync import sync_emonbestlogs_products

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync EmonBestLogs supplier products into the local product catalog."

    def handle(self, *args, **options):
        try:
            result = sync_emonbestlogs_products()
            self.stdout.write(self.style.SUCCESS(
                f"Categories created={result['categories_created']} updated={result['categories_updated']} "
                f"Products created={result['products_created']} updated={result['products_updated']} "
                f"SupplierProducts created={result['supplier_products_created']} updated={result['supplier_products_updated']} "
                f"Failed={result['failed']}"
            ))
        except Exception as exc:
            logger.exception("Supplier product sync failed.")
            raise CommandError(str(exc)) from exc
