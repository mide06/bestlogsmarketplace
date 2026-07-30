import logging
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from BestLogMarketPlaceApp.models import Category, Product, SupplierProduct
from BestLogMarketPlaceApp.services.emonbestlogs import EmonBestLogsAPIError, EmonBestLogsService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync EmonBestLogs supplier products into the local catalog."

    def add_arguments(self, parser):
        parser.add_argument("--page", type=int, default=1)
        parser.add_argument("--limit", type=int, default=0, help="Optional limit for how many products to sync.")

    def handle(self, *args, **options):
        try:
            service = EmonBestLogsService()
            response = service.get_products(page=options["page"])
            items = response.get("results") if isinstance(response, dict) else response

            if not isinstance(items, list):
                raise CommandError("Unexpected supplier response format while syncing products.")

            synced = 0
            for item in items:
                supplier_product_id = item.get("id") or item.get("product_id")
                if not supplier_product_id:
                    continue

                supplier_product_id = str(supplier_product_id)
                supplier_category_name = item.get("category") or item.get("category_name") or "General"
                local_category, _ = Category.objects.get_or_create(name=supplier_category_name)

                # Prepare canonical fields from supplier payload
                supplier_name = item.get("supplier_name") or item.get("name") or "EmonBestLogs"
                supplier_price = Decimal(str(item.get("price") or item.get("supplier_price") or 0.00))
                selling_price = Decimal(str(item.get("my_selling_price") or item.get("price") or 0.00))
                stock = int(item.get("stock") or item.get("in_stock") or 0)
                product_name = item.get("name") or item.get("product_name") or None
                account_details = item.get("description") or item.get("account_details") or ""
                view_link = item.get("view_link") or ""

                # Find existing supplier record
                supplier_obj = SupplierProduct.objects.filter(supplier_product_id=supplier_product_id).first()

                # Try to find an existing Product linked by supplier_product_id (backwards compatibility)
                product = Product.objects.filter(supplier_product_id=supplier_product_id).first()

                # If no product exists, create one only if supplier provides a valid name and selling price
                created_product = False
                if not product and product_name and selling_price and selling_price > 0:
                    product = Product.objects.create(
                        name=product_name,
                        price=selling_price,
                        category=local_category,
                        account_details=account_details,
                        view_link=view_link,
                        is_active=bool(item.get("active", True)),
                        supplier_product_id=int(supplier_product_id) if supplier_product_id.isdigit() else None,
                        supplier_price=supplier_price,
                        supplier_name=supplier_name,
                        supplier_stock=stock,
                        supplier_synced_at=timezone.now(),
                    )
                    created_product = True

                # Create or update supplier metadata record
                if supplier_obj:
                    supplier_obj.supplier_name = supplier_name
                    supplier_obj.supplier_price = supplier_price
                    supplier_obj.supplier_stock = stock
                    supplier_obj.supplier_synced_at = timezone.now()
                    supplier_obj.supplier_response = item
                    if product:
                        supplier_obj.product = product
                    supplier_obj.raw_payload = item
                    supplier_obj.save()
                else:
                    SupplierProduct.objects.create(
                        product=product,
                        supplier_product_id=supplier_product_id,
                        supplier_name=supplier_name,
                        supplier_price=supplier_price,
                        supplier_stock=stock,
                        supplier_synced_at=timezone.now(),
                        supplier_response=item,
                        raw_payload=item,
                    )

                # Keep backward-compatible product fields in sync when a product exists
                if product and not created_product:
                    # Only update non-essential storefront fields to avoid overwriting curated data
                    updated = False
                    if product.supplier_price != supplier_price:
                        product.supplier_price = supplier_price
                        updated = True
                    if product.supplier_stock != stock:
                        product.supplier_stock = stock
                        updated = True
                    if not product.view_link and view_link:
                        product.view_link = view_link
                        updated = True
                    if updated:
                        product.supplier_synced_at = timezone.now()
                        product.save()

                synced += 1

                if options["limit"] and synced >= options["limit"]:
                    break

            self.stdout.write(self.style.SUCCESS(f"Successfully synced {synced} products from EmonBestLogs."))
        except EmonBestLogsAPIError as exc:
            logger.exception("Failed to sync EmonBestLogs products.")
            raise CommandError(str(exc)) from exc
