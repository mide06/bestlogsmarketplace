import logging
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from BestLogMarketPlaceApp.models import Category, Product, SupplierProduct
from BestLogMarketPlaceApp.services.emonbestlogs import EmonBestLogsAPIError, EmonBestLogsService
from django.utils.text import slugify

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync EmonBestLogs supplier products into the local product catalog."

    def handle(self, *args, **options):
        try:
            service = EmonBestLogsService()
            categories_payload = service.get_categories()
            supplier_categories = categories_payload.get("results") if isinstance(categories_payload, dict) else categories_payload

            category_map = {}
            if isinstance(supplier_categories, list):
                for cat_data in supplier_categories:
                    supplier_category_id = str(cat_data.get("id") or cat_data.get("category_id") or "").strip()
                    category_name = cat_data.get("name") or cat_data.get("category_name") or f"Category {supplier_category_id or 'unknown'}"
                    slug_base = slugify(category_name) or f"category-{supplier_category_id or 'unknown'}"
                    slug = slug_base
                    suffix = 2
                    while slug and Category.objects.filter(slug=slug).exclude(supplier_category_id=supplier_category_id).exists():
                        slug = f"{slug_base}-{suffix}"
                        suffix += 1

                    category = None
                    if supplier_category_id:
                        category = Category.objects.filter(supplier_category_id=supplier_category_id).first()
                    if not category:
                        category = Category.objects.filter(slug=slug).first() or Category.objects.filter(name__iexact=category_name).first()

                    if not category:
                        category = Category.objects.create(
                            name=category_name,
                            slug=slug,
                            supplier_category_id=supplier_category_id,
                        )
                    else:
                        updated = False
                        if not category.slug:
                            category.slug = slug
                            updated = True
                        # Do not overwrite a manually maintained category description
                        if supplier_category_id and not category.supplier_category_id:
                            category.supplier_category_id = supplier_category_id
                            updated = True
                        if updated:
                            category.save()

                    if supplier_category_id:
                        category_map[supplier_category_id] = category

            payload = service.get_products()
            products = payload.get("results") if isinstance(payload, dict) else payload

            if not isinstance(products, list):
                raise CommandError("Unexpected response format returned by EmonBestLogs.")

            default_category, _ = Category.objects.get_or_create(name="General", defaults={"slug": "general"})
            synced = 0
            for item in products:
                supplier_product_id = item.get("id") or item.get("product_id")
                if not supplier_product_id:
                    continue

                supplier_product_id = str(supplier_product_id)
                supplier_category_id = str(item.get("category_id") or item.get("category") or item.get("category_name") or "").strip()
                category = category_map.get(supplier_category_id) if supplier_category_id else None
                if not category and supplier_category_id:
                    category = Category.objects.filter(supplier_category_id=supplier_category_id).first()

                if not category:
                    category_name = item.get("category") or item.get("category_name")
                    if category_name:
                        category = Category.objects.filter(name__iexact=category_name).first()

                if not category:
                    category = default_category

                supplier_price = Decimal(str(item.get("supplier_price") or item.get("price") or 0.00))
                my_selling_price_raw = item.get("my_selling_price")
                my_selling_price = Decimal(str(my_selling_price_raw)) if my_selling_price_raw is not None else None

                raw_stock = item.get("stock")
                in_stock_flag = item.get("in_stock")
                if raw_stock is None:
                    if isinstance(in_stock_flag, bool):
                        stock = 1 if in_stock_flag else 0
                    else:
                        try:
                            stock = int(str(in_stock_flag))
                        except (TypeError, ValueError):
                            stock = 0
                else:
                    try:
                        stock = int(raw_stock)
                    except (TypeError, ValueError):
                        stock = 0

                product_name = item.get("name") or item.get("product_name") or None
                account_details = item.get("description") or item.get("account_details") or ""
                view_link = item.get("view_link") or ""

                available = stock > 0
                if item.get("active") is False:
                    available = False

                supplier_obj = SupplierProduct.objects.filter(supplier_product_id=supplier_product_id).first()
                product = Product.objects.filter(supplier_product_id=supplier_product_id).first()

                created_product = False
                if not product and product_name:
                    price_to_set = my_selling_price if my_selling_price is not None else (supplier_price if supplier_price and supplier_price > 0 else Decimal('0.00'))
                    product = Product.objects.create(
                        name=product_name,
                        price=price_to_set,
                        category=category,
                        account_details=account_details,
                        view_link=view_link,
                        is_active=available,
                        supplier_product_id=supplier_product_id,
                        supplier_price=supplier_price,
                        supplier_name=item.get("supplier_name") or item.get("name") or "EmonBestLogs",
                        supplier_stock=stock,
                        supplier_synced_at=timezone.now(),
                    )
                    created_product = True

                if supplier_obj:
                    supplier_obj.supplier_name = item.get("supplier_name") or item.get("name") or supplier_obj.supplier_name or "EmonBestLogs"
                    supplier_obj.supplier_price = supplier_price
                    supplier_obj.supplier_stock = stock
                    supplier_obj.supplier_category_id = supplier_category_id or supplier_obj.supplier_category_id
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
                        supplier_category_id=supplier_category_id or None,
                        supplier_name=item.get("supplier_name") or item.get("name") or "EmonBestLogs",
                        supplier_price=supplier_price,
                        supplier_stock=stock,
                        supplier_synced_at=timezone.now(),
                        supplier_response=item,
                        raw_payload=item,
                    )

                if product and not created_product:
                    updated = False
                    if product.supplier_price != supplier_price:
                        product.supplier_price = supplier_price
                        updated = True
                    if product.supplier_stock != stock:
                        product.supplier_stock = stock
                        updated = True
                    if product.is_active != available:
                        product.is_active = available
                        updated = True
                    if not product.view_link and view_link:
                        product.view_link = view_link
                        updated = True
                    if supplier_category_id and product.category != category:
                        product.category = category
                        updated = True
                    if my_selling_price is not None and (product.price == 0 or product.price is None):
                        product.price = my_selling_price
                        updated = True
                    if updated:
                        product.supplier_synced_at = timezone.now()
                        product.save()

                synced += 1

            self.stdout.write(self.style.SUCCESS(f"Synced {synced} supplier products successfully."))
        except EmonBestLogsAPIError as exc:
            logger.exception("Supplier product sync failed.")
            raise CommandError(str(exc)) from exc
