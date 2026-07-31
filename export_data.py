import json
import os
import shutil
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "BestLogMarketPlaceProject.settings")

import django
from django.conf import settings
from django.db.models import Model
from django.db.models.fields import files
from django.db.models.fields.related import ForeignKey, ManyToManyField, OneToOneField


django.setup()

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
OUTPUT_PATH = BASE_DIR / "data.json"


def backup_sqlite_database():
    db_config = settings.DATABASES.get("default", {})
    db_name = db_config.get("NAME")
    if not db_name:
        raise RuntimeError("No database NAME configured in settings.")

    db_path = Path(db_name)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database file not found: {db_path}")

    if db_path.suffix.lower() != ".sqlite3":
        raise RuntimeError(f"Expected SQLite database, but found: {db_path}")

    backup_path = BACKUP_DIR / f"db.sqlite3.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
    shutil.copy2(db_path, backup_path)
    print(f"Backup created: {backup_path}")


def serialize_value(value, model_label, field_name):
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(k): serialize_value(v, model_label, field_name) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [serialize_value(item, model_label, field_name) for item in value]

    if isinstance(value, Model):
        return {"model": value._meta.label_lower, "pk": value.pk}

    if isinstance(value, files.FieldFile):
        return str(value.name) if value.name else None

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    return str(value)


def export_model(model):
    objects = []
    queryset = model.objects.all().order_by("pk")

    for instance in queryset:
        fields = {}
        for field in model._meta.get_fields():
            if field.name == "id" and field.primary_key:
                continue

            if field.auto_created and field.concrete is False:
                continue

            if not getattr(field, "concrete", False):
                continue

            if field.many_to_many:
                try:
                    related_values = [
                        {"model": rel._meta.label_lower, "pk": rel.pk}
                        for rel in getattr(instance, field.name).all()
                    ]
                    fields[field.name] = related_values
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to serialize many-to-many field {model._meta.label_lower}.{field.name} (pk={instance.pk}): {exc}"
                    ) from exc
                continue

            if isinstance(field, (ForeignKey, OneToOneField)):
                try:
                    related_value = getattr(instance, field.name)
                    if related_value is None:
                        fields[field.name] = None
                    else:
                        fields[field.name] = {
                            "model": related_value._meta.label_lower,
                            "pk": related_value.pk,
                        }
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to serialize relation {model._meta.label_lower}.{field.name} (pk={instance.pk}): {exc}"
                    ) from exc
                continue

            try:
                raw_value = getattr(instance, field.name)
                fields[field.name] = serialize_value(raw_value, model._meta.label_lower, field.name)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to serialize field {model._meta.label_lower}.{field.name} (pk={instance.pk}): {exc}"
                ) from exc

        objects.append({
            "model": model._meta.label_lower,
            "pk": instance.pk,
            "fields": fields,
        })

    return objects


def export_all_models():
    exported = []
    for model in django.apps.apps.get_models():
        model_label = model._meta.label_lower
        if model_label.startswith("contenttypes"):
            continue

        if model_label == "auth.permission":
            continue

        exported.extend(export_model(model))

    return exported


def main():
    backup_sqlite_database()

    exported_objects = export_all_models()

    with OUTPUT_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(exported_objects, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    from django.db import connection
    print(f"Database engine: {connection.vendor}")
    print(f"Exported file: {OUTPUT_PATH}")
    print(f"Users exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'CustomUser').objects.count()}")
    print(f"Categories exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'Category').objects.count()}")
    print(f"Products exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'Product').objects.count()}")
    print(f"Supplier products exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'SupplierProduct').objects.count()}")
    print(f"Transactions exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'Transaction').objects.count()}")
    print(f"Carts exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'Cart').objects.count()}")
    print(f"Cart items exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'CartItem').objects.count()}")
    print(f"Bank payment details exported: {django.apps.apps.get_model('BestLogMarketPlaceApp', 'BankPaymentDetail').objects.count()}")
    print(f"Sessions exported: {django.apps.apps.get_model('sessions', 'session').objects.count()}")
    print(f"Total objects exported: {len(exported_objects)}")


if __name__ == "__main__":
    main()
