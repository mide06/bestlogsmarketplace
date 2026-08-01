import traceback
import logging
import traceback
from decimal import Decimal

from django.utils import timezone
from django.utils.text import slugify

from BestLogMarketPlaceApp.models import Category, Product, SupplierProduct
from BestLogMarketPlaceApp.services.emonbestlogs import EmonBestLogsAPIError, EmonBestLogsService

logger = logging.getLogger(__name__)


def _parse_stock(item):
    raw_stock = item.get("stock")
    in_stock_flag = item.get("in_stock")
    if raw_stock is None:
        if isinstance(in_stock_flag, bool):
            return 1 if in_stock_flag else 0
        try:
            return int(str(in_stock_flag))
        except (TypeError, ValueError):
            return 0
    try:
        return int(raw_stock)
    except (TypeError, ValueError):
        return 0


def _parse_decimal(value):
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, Decimal.InvalidOperation):
        return Decimal("0.00")


def _build_category_slug(base):
    slug = slugify(base) or "category"
    suffix = 2
    while Category.objects.filter(slug=slug).exists():
        slug = f"{slugify(base)}-{suffix}"
        suffix += 1
    return slug


def _extract_payload_list(payload, keys, payload_name):
    if isinstance(payload, list):
        logger.info("EmonBestLogs %s payload is a list. length=%s", payload_name, len(payload))
        logger.info("EmonBestLogs %s payload raw: %s", payload_name, payload)
        return payload

    if not isinstance(payload, dict):
        logger.error("EmonBestLogs %s payload unexpected type: %s", payload_name, type(payload))
        logger.error("EmonBestLogs %s payload raw: %s", payload_name, payload)
        return []

    for key in keys:
        if key in payload and isinstance(payload[key], list):
            logger.info("EmonBestLogs %s payload key '%s' found. length=%s", payload_name, key, len(payload[key]))
            logger.info("EmonBestLogs %s payload raw: %s", payload_name, payload[key])
            return payload[key]

    logger.warning("EmonBestLogs %s payload keys not matched. available keys=%s", payload_name, list(payload.keys()))
    logger.warning("EmonBestLogs %s payload raw: %s", payload_name, payload)
    return []


def sync_emonbestlogs_products():
    logger.info("Starting supplier sync...")
    result = {
        "categories_created": 0,
        "categories_updated": 0,
        "products_created": 0,
        "products_updated": 0,
        "supplier_products_created": 0,
        "supplier_products_updated": 0,
        "failed": 0,
        "categories_received": 0,
        "products_received": 0,
    }

    service = EmonBestLogsService()
    categories_payload = service.get_categories()
    supplier_categories = _extract_payload_list(categories_payload, ["results", "data", "categories"], "categories")
    result["categories_received"] = len(supplier_categories) if isinstance(supplier_categories, list) else 0
    logger.info("Number of categories received: %s", result["categories_received"])

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

            defaults = {
                "name": category_name,
                "slug": slug,
            }
            if supplier_category_id:
                category, created = Category.objects.update_or_create(
                    supplier_category_id=supplier_category_id,
                    defaults=defaults,
                )
                if created:
                    result["categories_created"] += 1
                else:
                    result["categories_updated"] += 1
            else:
                category = Category.objects.filter(slug=slug).first() or Category.objects.filter(name__iexact=category_name).first()
                if category:
                    updated = False
                    if not category.slug:
                        category.slug = slug
                        updated = True
                    if updated:
                        category.save()
                        result["categories_updated"] += 1
                else:
                    category = Category.objects.create(name=category_name, slug=slug)
                    result["categories_created"] += 1

            if supplier_category_id:
                category_map[supplier_category_id] = category

    payload = service.get_products()
    products = _extract_payload_list(payload, ["results", "data", "products"], "products")
    result["products_received"] = len(products) if isinstance(products, list) else 0
    logger.info("Number of products received: %s", result["products_received"])

    if not isinstance(products, list):
        raise EmonBestLogsAPIError(
            f"Unexpected response format returned by EmonBestLogs. payload keys={list(payload.keys()) if isinstance(payload, dict) else type(payload)}"
        )

    default_category, _ = Category.objects.get_or_create(name="General", defaults={"slug": "general"})

    for item in products:
        try:
            supplier_product_id = item.get("id") or item.get("product_id")
            if not supplier_product_id:
                logger.warning("Skipping product because supplier_product_id is missing. payload=%s", item)
                result["failed"] += 1
                continue

            supplier_product_id = str(supplier_product_id)
            supplier_category_id = str(item.get("category_id") or item.get("category") or item.get("category_name") or "").strip()
            category = category_map.get(supplier_category_id) if supplier_category_id else None
            if not category and supplier_category_id:
                category = Category.objects.filter(supplier_category_id=supplier_category_id).first()

            product_name = item.get("name") or item.get("product_name") or None
            if not product_name:
                logger.warning("Skipping product because name is missing. supplier_product_id=%s payload=%s", supplier_product_id, item)
                result["failed"] += 1
                continue

            logger.info(
                "Processing supplier product: supplier_id=%s name=%s category=%s stock=%s price=%s",
                supplier_product_id,
                product_name,
                supplier_category_id or "(none)",
                _parse_stock(item),
                item.get("supplier_price") or item.get("price"),
            )

            if not category:
                category_name = item.get("category") or item.get("category_name")
                if category_name:
                    category = Category.objects.filter(name__iexact=category_name).first()

            if not category:
                category_name = item.get("category") or item.get("category_name")
                if category_name:
                    category = Category.objects.filter(name__iexact=category_name).first()

            if not category:
                category = default_category

            supplier_price = _parse_decimal(item.get("supplier_price") or item.get("price") or 0.00)
            my_selling_price_raw = item.get("my_selling_price")
            my_selling_price = _parse_decimal(my_selling_price_raw) if my_selling_price_raw is not None else None
            stock = _parse_stock(item)

            product_name = item.get("name") or item.get("product_name") or None
            account_details = item.get("description") or item.get("account_details") or ""
            view_link = item.get("view_link") or ""
            available = stock > 0
            if item.get("active") is False:
                available = False

            if not product_name:
                result["failed"] += 1
                continue

            price_to_set = my_selling_price if my_selling_price is not None else (supplier_price if supplier_price and supplier_price > 0 else Decimal("0.00"))
            product_defaults = {
                "name": product_name,
                "price": price_to_set,
                "category": category,
                "account_details": account_details,
                "view_link": view_link,
                "is_active": available,
                "supplier_price": supplier_price,
                "supplier_name": item.get("supplier_name") or item.get("name") or "EmonBestLogs",
                "supplier_stock": stock,
                "supplier_synced_at": timezone.now(),
            }

            product, created = Product.objects.update_or_create(
                supplier_product_id=supplier_product_id,
                defaults={**product_defaults, "supplier_product_id": supplier_product_id},
            )
            if created:
                result["products_created"] += 1
            else:
                result["products_updated"] += 1

            supplier_defaults = {
                "product": product,
                "supplier_name": item.get("supplier_name") or item.get("name") or "EmonBestLogs",
                "supplier_price": supplier_price,
                "supplier_stock": stock,
                "supplier_synced_at": timezone.now(),
                "supplier_response": item,
                "raw_payload": item,
            }
            supplier_obj, supplier_created = SupplierProduct.objects.update_or_create(
                supplier_product_id=supplier_product_id,
                defaults=supplier_defaults,
            )
            if supplier_created:
                result["supplier_products_created"] += 1
            else:
                result["supplier_products_updated"] += 1

        except Exception as e:
            result["failed"] += 1
            logger.exception("Failed to import product supplier_id=%s payload=%s", supplier_product_id if 'supplier_product_id' in locals() else None, item)
            continue

    logger.info("EmonBestLogs sync summary: categories_returned=%s products_returned=%s categories_created=%s categories_updated=%s products_created=%s products_updated=%s supplier_products_created=%s supplier_products_updated=%s failed=%s",
        len(supplier_categories) if isinstance(supplier_categories, list) else 0,
        len(products) if isinstance(products, list) else 0,
        result['categories_created'],
        result['categories_updated'],
        result['products_created'],
        result['products_updated'],
        result['supplier_products_created'],
        result['supplier_products_updated'],
        result['failed'],
    )

    return result
