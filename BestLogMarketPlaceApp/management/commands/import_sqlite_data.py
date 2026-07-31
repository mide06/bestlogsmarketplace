import json
import os
from datetime import datetime
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models.fields.related import ForeignKey, ManyToManyField, OneToOneField
from django.contrib.sessions.models import Session

from BestLogMarketPlaceApp.models import (
    BankPaymentDetail,
    Cart,
    CartItem,
    Category,
    CustomUser,
    DataImportMarker,
    Product,
    SupplierProduct,
    Transaction,
)


class Command(BaseCommand):
    help = 'Import exported SQLite data.json into PostgreSQL safely without duplicating records.'

    def add_arguments(self, parser):
        parser.add_argument('--input-file', default='data.json', help='Path to the exported JSON file.')
        parser.add_argument('--dry-run', action='store_true', help='Validate the JSON and report what would be imported without changing the database.')
        parser.add_argument('--verify-only', action='store_true', help='Report the current counts after import.')

    def handle(self, *args, **options):
        input_file = options['input_file']
        dry_run = options['dry_run']
        verify_only = options['verify_only']

        if verify_only:
            self.report_counts()
            return

        if connection.vendor != 'postgresql':
            raise CommandError('This import command only runs against PostgreSQL. Current database engine: %s' % connection.vendor)

        if not os.path.exists(input_file):
            raise CommandError(f'Import file not found: {input_file}')

        self.stdout.write(self.style.WARNING('Loading JSON payload...'))
        try:
            with open(input_file, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
        except Exception as exc:
            raise CommandError(f'Failed to parse JSON: {exc}')

        if not isinstance(payload, list):
            raise CommandError('Expected the import file to contain a top-level JSON array.')

        if dry_run:
            self.run_dry_run(payload)
            return

        if DataImportMarker.objects.filter(name='sqlite_to_postgres').exists():
            self.stdout.write(self.style.WARNING('Import marker already exists; skipping import.'))
            self.report_counts()
            return

        self.stdout.write(self.style.WARNING('Applying pending migrations...'))
        call_command('migrate', verbosity=0, interactive=False)

        self.stdout.write(self.style.WARNING('Starting transactional import...'))
        with transaction.atomic():
            self.import_payload(payload)
            DataImportMarker.objects.create(
                name='sqlite_to_postgres',
                source_file=input_file,
                total_objects=len(payload),
            )
            self.reset_sequences()

        self.stdout.write(self.style.SUCCESS('Import completed successfully.'))
        self.report_counts()

    def run_dry_run(self, payload):
        self.stdout.write(self.style.WARNING('Dry run: validating structure and dependencies only.'))
        if not isinstance(payload, list):
            raise CommandError('Expected a list of exported objects.')

        seen_pks = {}
        payload_index = self.build_payload_index(payload)
        for index, entry in enumerate(payload, 1):
            if not isinstance(entry, dict):
                raise CommandError(f'Entry {index} is not an object.')
            model_label = entry.get('model')
            if not model_label:
                raise CommandError(f'Entry {index} is missing a model label.')
            if not self.model_exists(model_label):
                raise CommandError(f'Entry {index} references unknown model: {model_label}')
            if 'pk' not in entry:
                raise CommandError(f'Entry {index} is missing a primary key.')
            if 'fields' not in entry or not isinstance(entry['fields'], dict):
                raise CommandError(f'Entry {index} is missing a fields object.')
            model_class = self.get_model_class(model_label)
            if model_class is None:
                raise CommandError(f'Entry {index} uses an unknown model: {model_label}')
            pk = entry['pk']
            if pk in seen_pks:
                raise CommandError(f'Duplicate primary key {pk} for model {model_label}.')
            seen_pks[pk] = model_label

            self.validate_fields(model_class, entry['fields'], payload_index)

        self.stdout.write(self.style.SUCCESS('Dry run validation passed.'))
        self.stdout.write(self.style.SUCCESS('The import would process %s objects.' % len(payload)))

    def import_payload(self, payload):
        model_order = [
            'BestLogMarketPlaceApp.customuser',
            'BestLogMarketPlaceApp.category',
            'BestLogMarketPlaceApp.product',
            'BestLogMarketPlaceApp.supplierproduct',
            'BestLogMarketPlaceApp.transaction',
            'BestLogMarketPlaceApp.cart',
            'BestLogMarketPlaceApp.cartitem',
            'BestLogMarketPlaceApp.bankpaymentdetail',
        ]

        model_map = {self.get_model_class(label)._meta.label_lower: self.get_model_class(label) for label in model_order if self.get_model_class(label) is not None}
        import_map = {label: [] for label in model_order}
        skipped = []

        for entry in payload:
            model_label = entry.get('model')
            if model_label in {'auth.permission', 'contenttypes.contenttype', 'auth.group', 'django_admin_log', 'sessions.session'}:
                skipped.append(model_label)
                continue
            if model_label in {'admin.logentry', 'auth.permission', 'contenttypes.contenttype', 'auth.group'}:
                skipped.append(model_label)
                continue
            if model_label.startswith('auth.') or model_label.startswith('contenttypes.') or model_label.startswith('django.contrib.'):
                skipped.append(model_label)
                continue

            model_class = self.get_model_class(model_label)
            if model_class is None:
                continue

            if model_label not in model_map:
                continue
            import_map[model_label].append(entry)

        for label in model_order:
            for entry in import_map.get(label, []):
                self.import_single_entry(label, entry)

        self.stdout.write(self.style.WARNING('Skipped framework-generated records: %s' % ', '.join(sorted(set(skipped))) if skipped else 'none'))

    def import_single_entry(self, label, entry):
        model_class = self.get_model_class(label)
        if model_class is None:
            return

        pk = entry.get('pk')
        fields = entry.get('fields', {})
        defaults = {}
        for field_name, raw_value in fields.items():
            if field_name in {'id'}:
                continue
            if field_name.endswith('_ptr'):
                continue
            if raw_value is None:
                defaults[field_name] = None
                continue
            defaults[field_name] = self.coerce_field_value(model_class, field_name, raw_value)

        if model_class.objects.filter(pk=pk).exists():
            self.stdout.write(self.style.WARNING(f'Skipping existing {label} pk={pk}'))
            return

        instance = model_class(pk=pk, **defaults)
        instance.save(force_insert=True)

        if label == 'BestLogMarketPlaceApp.transaction':
            self.sync_transaction_m2m(instance, fields)

    def sync_transaction_m2m(self, instance, fields):
        products = fields.get('products', [])
        if not products:
            return
        product_ids = []
        for item in products:
            if isinstance(item, dict):
                product_ids.append(item.get('pk'))
        if product_ids:
            valid_ids = [product_id for product_id in product_ids if product_id is not None and Product.objects.filter(pk=product_id).exists()]
            instance.products.set(valid_ids)

    def coerce_field_value(self, model_class, field_name, raw_value):
        field = model_class._meta.get_field(field_name)
        if isinstance(field, ManyToManyField):
            return []
        if isinstance(field, ForeignKey):
            if raw_value is None:
                return None
            if isinstance(raw_value, dict):
                related_model = field.remote_field.model
                related_pk = raw_value.get('pk')
                if related_model is None:
                    return None
                if not related_model.objects.filter(pk=related_pk).exists():
                    related_model.objects.get_or_create(pk=related_pk)
                return related_model.objects.get(pk=related_pk)
            return raw_value
        if isinstance(field, OneToOneField):
            if raw_value is None:
                return None
            if isinstance(raw_value, dict):
                related_model = field.remote_field.model
                related_pk = raw_value.get('pk')
                if not related_model.objects.filter(pk=related_pk).exists():
                    related_model.objects.get_or_create(pk=related_pk)
                return related_model.objects.get(pk=related_pk)
            return raw_value
        if field.get_internal_type() == 'DecimalField':
            return Decimal(str(raw_value)) if raw_value is not None else None
        if field.get_internal_type() == 'DateTimeField' and isinstance(raw_value, str):
            return datetime.fromisoformat(raw_value.replace('Z', '+00:00'))
        if field.get_internal_type() == 'DateField' and isinstance(raw_value, str):
            return datetime.fromisoformat(raw_value).date()
        if field.get_internal_type() == 'TimeField' and isinstance(raw_value, str):
            return datetime.fromisoformat(raw_value).time()
        if field.get_internal_type() == 'BooleanField':
            return bool(raw_value)
        if field.get_internal_type() == 'JSONField':
            return raw_value if isinstance(raw_value, (dict, list)) else json.loads(raw_value)
        return raw_value

    def validate_fields(self, model_class, fields, payload_index):
        for field_name, raw_value in fields.items():
            if field_name in {'id'}:
                continue
            if field_name.endswith('_ptr'):
                continue
            if raw_value is None:
                continue
            field = model_class._meta.get_field(field_name)
            if isinstance(field, ForeignKey):
                if isinstance(raw_value, dict):
                    related_model = field.remote_field.model
                    related_pk = raw_value.get('pk')
                    if related_model and related_pk is not None:
                        related_label = related_model._meta.label_lower
                        if related_label not in payload_index:
                            raise CommandError(f'{model_class._meta.label_lower}.{field_name} references missing model {related_label}')
                        if related_pk not in payload_index[related_label]:
                            raise CommandError(f'{model_class._meta.label_lower}.{field_name} references missing related object pk={related_pk}')
            if isinstance(field, ManyToManyField):
                continue

    def build_payload_index(self, payload):
        index = {}
        for entry in payload:
            model_label = entry.get('model')
            if not model_label:
                continue
            pk = entry.get('pk')
            if pk is None:
                continue
            index.setdefault(model_label, set()).add(pk)
        return index

    def model_exists(self, label):
        try:
            self.get_model_class(label)
            return True
        except CommandError:
            return False

    def get_model_class(self, label):
        model_map = {
            'BestLogMarketPlaceApp.customuser': CustomUser,
            'BestLogMarketPlaceApp.category': Category,
            'BestLogMarketPlaceApp.product': Product,
            'BestLogMarketPlaceApp.supplierproduct': SupplierProduct,
            'BestLogMarketPlaceApp.transaction': Transaction,
            'BestLogMarketPlaceApp.cart': Cart,
            'BestLogMarketPlaceApp.cartitem': CartItem,
            'BestLogMarketPlaceApp.bankpaymentdetail': BankPaymentDetail,
        }
        if label in model_map:
            return model_map[label]
        raise CommandError(f'Unsupported model label: {label}')

    def report_counts(self):
        self.stdout.write(self.style.SUCCESS('Current counts:'))
        self.stdout.write(f'CustomUser: {CustomUser.objects.count()}')
        self.stdout.write(f'Category: {Category.objects.count()}')
        self.stdout.write(f'Product: {Product.objects.count()}')
        self.stdout.write(f'SupplierProduct: {SupplierProduct.objects.count()}')
        self.stdout.write(f'Transaction: {Transaction.objects.count()}')
        self.stdout.write(f'Cart: {Cart.objects.count()}')
        self.stdout.write(f'CartItem: {CartItem.objects.count()}')
        self.stdout.write(f'BankPaymentDetail: {BankPaymentDetail.objects.count()}')
        self.stdout.write(f'Session: {Session.objects.count()}')
        self.stdout.write(f'DataImportMarker: {DataImportMarker.objects.count()}')

    def reset_sequences(self):
        with connection.cursor() as cursor:
            for model in [CustomUser, Category, Product, SupplierProduct, Transaction, Cart, CartItem, BankPaymentDetail]:
                sequence_name = f'{model._meta.db_table}_id_seq'
                cursor.execute("SELECT setval(%s, COALESCE((SELECT MAX(id) FROM %s), 1), true)", [sequence_name, model._meta.db_table])
