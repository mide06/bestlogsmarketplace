import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import ijson
from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django.db.models.fields.related import ForeignKey, ManyToManyField, OneToOneField

from .models import (
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

DEFAULT_TEMP_IMPORT_DIR = Path(settings.BASE_DIR) / '.tmp_imports'
TEMP_IMPORT_DIR = Path(os.getenv('DATA_IMPORT_DIR', str(DEFAULT_TEMP_IMPORT_DIR))).expanduser().resolve()
UPLOAD_FILE = (TEMP_IMPORT_DIR / 'data.json').resolve()
PROGRESS_FILE = (TEMP_IMPORT_DIR / 'import_progress.json').resolve()
CHUNK_DIR = (TEMP_IMPORT_DIR / 'chunks').resolve()
BATCH_SIZE_DEFAULT = 200
MAX_BATCH_SIZE = 500
CHUNK_NAME_PATTERN = re.compile(r'^(users|categories|products|supplier_products|bank_payment_details|transactions|carts|cart_items)-\d{4,}\.json$')

IMPORT_STAGES = [
    ('users', ['BestLogMarketPlaceApp.customuser']),
    ('categories', ['BestLogMarketPlaceApp.category']),
    ('products', ['BestLogMarketPlaceApp.product']),
    ('supplier_products', ['BestLogMarketPlaceApp.supplierproduct']),
    ('bank_payment_details', ['BestLogMarketPlaceApp.bankpaymentdetail']),
    ('transactions', ['BestLogMarketPlaceApp.transaction']),
    ('carts', ['BestLogMarketPlaceApp.cart']),
    ('cart_items', ['BestLogMarketPlaceApp.cartitem']),
]

SKIPPED_MODEL_PREFIXES = ('auth.', 'contenttypes.', 'django.contrib.')
SKIPPED_MODEL_LABELS = {
    'admin.logentry',
    'sessions.session',
    'auth.permission',
    'auth.group',
    'contenttypes.contenttype',
}

MODEL_MAP = {
    'BestLogMarketPlaceApp.customuser': CustomUser,
    'BestLogMarketPlaceApp.category': Category,
    'BestLogMarketPlaceApp.product': Product,
    'BestLogMarketPlaceApp.supplierproduct': SupplierProduct,
    'BestLogMarketPlaceApp.transaction': Transaction,
    'BestLogMarketPlaceApp.cart': Cart,
    'BestLogMarketPlaceApp.cartitem': CartItem,
    'BestLogMarketPlaceApp.bankpaymentdetail': BankPaymentDetail,
}

logger = logging.getLogger(__name__)


class ImportErrorDetail(Exception):
    pass


def ensure_temp_import_dir():
    TEMP_IMPORT_DIR.mkdir(exist_ok=True, parents=True)
    CHUNK_DIR.mkdir(exist_ok=True, parents=True)


ensure_temp_import_dir()


def load_progress():
    if not PROGRESS_FILE.exists():
        return None
    with PROGRESS_FILE.open('r', encoding='utf-8') as handle:
        return json.load(handle)


def save_progress(progress):
    ensure_temp_import_dir()
    with PROGRESS_FILE.open('w', encoding='utf-8') as handle:
        json.dump(progress, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def get_recorded_file_path(progress=None):
    if progress and progress.get('data_file'):
        return Path(progress['data_file']).expanduser().resolve()
    return UPLOAD_FILE


def is_skipped_model(label):
    if not isinstance(label, str):
        return True
    if label in SKIPPED_MODEL_LABELS:
        return True
    return label.startswith(SKIPPED_MODEL_PREFIXES)


def is_importable_model(label):
    if not isinstance(label, str):
        return False
    return label in MODEL_MAP


def stream_json_objects(file_path):
    with open(file_path, 'r', encoding='utf-8') as handle:
        for item in ijson.items(handle, 'item'):
            yield item


def stage_for_chunk_name(chunk_name):
    if not CHUNK_NAME_PATTERN.fullmatch(chunk_name):
        raise ImportErrorDetail('Invalid chunk name. Use a generated stage-NNNN.json name.')
    return chunk_name.rsplit('-', 1)[0]


def validate_chunk_file(file_path, stage_name):
    expected_labels = dict(IMPORT_STAGES)[stage_name]
    total_objects = 0
    for index, entry in enumerate(stream_json_objects(file_path), start=1):
        if not isinstance(entry, dict):
            raise ImportErrorDetail(f'Chunk entry {index} is not a JSON object.')
        if entry.get('model') not in expected_labels:
            raise ImportErrorDetail(
                f'Chunk entry {index} has model {entry.get("model")!r}; expected {expected_labels}.'
            )
        if 'pk' not in entry or 'fields' not in entry or not isinstance(entry['fields'], dict):
            raise ImportErrorDetail(f'Chunk entry {index} is missing pk or fields.')
        total_objects += 1
    return total_objects


def initialize_chunk_progress():
    return {
        'mode': 'chunks',
        'data_file': str(CHUNK_DIR),
        'uploaded_at': datetime.utcnow().isoformat() + 'Z',
        'status': 'uploaded',
        'total_file_objects': 0,
        'total_importable': 0,
        'stage_index': 0,
        'stage_name': IMPORT_STAGES[0][0],
        'current_chunk': None,
        'current_chunk_index': 0,
        'chunk_object_offset': 0,
        'batch_number': 0,
        'processed': 0,
        'successful': 0,
        'skipped': 0,
        'failed': 0,
        'failed_objects': [],
        'last_error': None,
        'chunks': {},
        'stage_complete': {},
        'completed_at': None,
    }


def register_chunk(chunk_name, stage_name, file_path, object_count):
    progress = load_progress()
    if progress is None or progress.get('mode') != 'chunks':
        progress = initialize_chunk_progress()
    record = progress['chunks'].get(chunk_name, {})
    progress['chunks'][chunk_name] = {
        'stage': stage_name,
        'path': str(file_path),
        'size': file_path.stat().st_size,
        'objects': object_count,
        'uploaded_at': record.get('uploaded_at', datetime.utcnow().isoformat() + 'Z'),
    }
    progress['total_file_objects'] = sum(item['objects'] for item in progress['chunks'].values())
    progress['total_importable'] = progress['total_file_objects']
    save_progress(progress)
    logger.info('Temporary import chunk registered: path=%s stage=%s objects=%s size=%s', file_path, stage_name, object_count, file_path.stat().st_size)
    return progress


def mark_stage_complete(stage_name):
    progress = load_progress()
    if not progress or progress.get('mode') != 'chunks':
        raise ImportErrorDetail('No chunk upload state exists.')
    progress['stage_complete'][stage_name] = True
    save_progress(progress)
    logger.info('Temporary import stage upload complete: stage=%s chunks=%s', stage_name, sum(1 for item in progress['chunks'].values() if item['stage'] == stage_name))
    return progress


def count_file_objects(file_path):
    total_file_objects = 0
    total_importable = 0
    for obj in stream_json_objects(file_path):
        total_file_objects += 1
        if is_importable_model(obj.get('model')):
            total_importable += 1
    return total_file_objects, total_importable


def initialize_upload(file_path):
    if DataImportMarker.objects.filter(name='sqlite_to_postgres').exists():
        raise ImportErrorDetail('Import has already completed; upload is not allowed.')

    if not file_path.exists():
        raise ImportErrorDetail('Uploaded file not found.')

    file_path = Path(file_path).resolve()
    total_file_objects, total_importable = count_file_objects(file_path)
    progress = {
        'data_file': str(UPLOAD_FILE),
        'uploaded_at': datetime.utcnow().isoformat() + 'Z',
        'status': 'uploaded',
        'total_file_objects': total_file_objects,
        'total_importable': total_importable,
        'stage_index': 0,
        'stage_name': IMPORT_STAGES[0][0],
        'batch_number': 0,
        'processed': 0,
        'successful': 0,
        'skipped': 0,
        'failed': 0,
        'failed_objects': [],
        'last_error': None,
        'completed_at': None,
    }
    save_progress(progress)
    return progress


def build_payload_index(file_path):
    index = {}
    for obj in stream_json_objects(file_path):
        label = obj.get('model')
        pk = obj.get('pk')
        if not is_importable_model(label) or pk is None:
            continue
        index.setdefault(label, set()).add(pk)
    return index


def validate_file(file_path):
    if not file_path.exists():
        raise ImportErrorDetail('Import file not found.')

    payload_index = build_payload_index(file_path)
    for index, entry in enumerate(stream_json_objects(file_path), start=1):
        if not isinstance(entry, dict):
            raise ImportErrorDetail(f'Entry {index} is not a JSON object.')
        model_label = entry.get('model')
        if not model_label:
            raise ImportErrorDetail(f'Entry {index} is missing a model label.')
        if is_skipped_model(model_label):
            continue
        if not is_importable_model(model_label):
            raise ImportErrorDetail(f'Entry {index} references unknown or unsupported model: {model_label}')
        if 'pk' not in entry:
            raise ImportErrorDetail(f'Entry {index} is missing a primary key.')
        if 'fields' not in entry or not isinstance(entry['fields'], dict):
            raise ImportErrorDetail(f'Entry {index} is missing a fields object.')

        for field_name, raw_value in entry['fields'].items():
            if raw_value is None:
                continue
            model_class = MODEL_MAP[model_label]
            if field_name.endswith('_ptr'):
                continue
            try:
                field = model_class._meta.get_field(field_name)
            except Exception:
                raise ImportErrorDetail(f'Entry {index} field {field_name} is not valid for model {model_label}')
            if isinstance(field, ForeignKey):
                if isinstance(raw_value, dict):
                    related_pk = raw_value.get('pk')
                    related_label = raw_value.get('model')
                    if related_pk is None or not related_label:
                        raise ImportErrorDetail(f'Entry {index} field {field_name} has invalid relation payload.')
                    if related_label not in payload_index or related_pk not in payload_index[related_label]:
                        raise ImportErrorDetail(
                            f'Entry {index} field {field_name} references missing related object {related_label} pk={related_pk}'
                        )

    return {
        'total_file_objects': count_file_objects(file_path)[0],
        'total_importable': len([1 for obj in stream_json_objects(file_path) if is_importable_model(obj.get('model'))]),
    }


def get_file_diagnostics(file_path=None):
    ensure_temp_import_dir()
    file_path = Path(file_path or UPLOAD_FILE).resolve()
    file_exists = file_path.is_file()
    return {
        'file_exists': file_exists,
        'file_path': str(file_path),
        'file_size': file_path.stat().st_size if file_exists else 0,
    }


def get_status():
    status = load_progress()
    if status is None:
        return {
            'status': 'no_upload',
            'detail': 'No uploaded import file exists in the current instance filesystem.',
            **get_file_diagnostics(),
            'upload_timestamp': None,
            'stage_name': None,
            'processed': 0,
            'successful': 0,
            'skipped': 0,
            'failed': 0,
        }
    status = status.copy()
    if status.get('mode') == 'chunks':
        current_chunk = status.get('current_chunk')
        current_path = status['chunks'].get(current_chunk, {}).get('path') if current_chunk else None
        status.update(get_file_diagnostics(current_path or CHUNK_DIR))
        status['remaining'] = max(0, status['total_importable'] - status['processed'])
        status['completed'] = status['status'] == 'completed'
        status['upload_timestamp'] = status.get('uploaded_at')
        status['progress_percent'] = int(status['processed'] / status['total_importable'] * 100) if status['total_importable'] else 0
        return status
    file_path = get_recorded_file_path(status)
    status['data_file'] = str(file_path)
    status.update(get_file_diagnostics(file_path))
    status['upload_timestamp'] = status.get('uploaded_at')
    status['remaining'] = max(0, status['total_importable'] - status['processed'])
    status['completed'] = status['status'] == 'completed'
    status['progress_percent'] = (
        int(status['processed'] / status['total_importable'] * 100)
        if status['total_importable'] else 0
    )
    return status


def get_stage_labels(stage_index):
    if 0 <= stage_index < len(IMPORT_STAGES):
        return IMPORT_STAGES[stage_index][1]
    return []


def collect_stage_batch(file_path, stage_labels, batch_size):
    batch = []
    stage_match_count = 0
    for obj in stream_json_objects(file_path):
        label = obj.get('model')
        if label not in stage_labels:
            continue
        if not is_importable_model(label):
            continue
        stage_match_count += 1
        if len(batch) < batch_size:
            batch.append(obj)
    return batch, stage_match_count


def coerce_field_value(model_class, field_name, raw_value):
    field = model_class._meta.get_field(field_name)
    if isinstance(field, ManyToManyField):
        return []
    if isinstance(field, ForeignKey) or isinstance(field, OneToOneField):
        if raw_value is None:
            return None
        if isinstance(raw_value, dict):
            related_pk = raw_value.get('pk')
            if related_pk is None:
                return None
            related_model = MODEL_MAP.get(raw_value.get('model'))
            if related_model is None:
                raise ImportErrorDetail(f'Missing related model for field {field_name}')
            try:
                return related_model.objects.get(pk=related_pk)
            except related_model.DoesNotExist:
                raise ImportErrorDetail(
                    f'Related object {related_model._meta.label_lower} pk={related_pk} not found'
                )
        return raw_value
    if field.get_internal_type() == 'DecimalField':
        return Decimal(str(raw_value))
    if field.get_internal_type() == 'DateTimeField' and isinstance(raw_value, str):
        return datetime.fromisoformat(raw_value.replace('Z', '+00:00'))
    if field.get_internal_type() == 'DateField' and isinstance(raw_value, str):
        return datetime.fromisoformat(raw_value).date()
    if field.get_internal_type() == 'TimeField' and isinstance(raw_value, str):
        return datetime.fromisoformat(raw_value).time()
    if field.get_internal_type() == 'BooleanField':
        return bool(raw_value)
    if field.get_internal_type() == 'JSONField':
        if isinstance(raw_value, (dict, list)):
            return raw_value
        return json.loads(raw_value)
    return raw_value


def resolve_many_to_many_value(field, raw_value):
    related_model = field.remote_field.model
    manager = related_model._default_manager
    if isinstance(raw_value, dict):
        if raw_value.get('pk') is not None:
            return manager.get(pk=raw_value['pk'])
        natural_values = raw_value.get('natural_key', raw_value.get('fields'))
        if natural_values is not None and hasattr(manager, 'get_by_natural_key'):
            if not isinstance(natural_values, (list, tuple)):
                natural_values = [natural_values]
            return manager.get_by_natural_key(*natural_values)
        raise ValueError(f'Unsupported relationship object: {raw_value!r}')
    if isinstance(raw_value, (list, tuple)):
        if hasattr(manager, 'get_by_natural_key'):
            return manager.get_by_natural_key(*raw_value)
        raise ValueError(f'Natural-key lookup is unavailable for {related_model._meta.label_lower}')
    return manager.get(pk=raw_value)


def apply_many_to_many_fields(instance, model_label, primary_key, m2m_payloads):
    for field, raw_values in m2m_payloads.items():
        values = [] if raw_values is None else raw_values
        if not isinstance(values, (list, tuple)):
            values = [values]
        try:
            related_objects = [resolve_many_to_many_value(field, value) for value in values]
            getattr(instance, field.name).set(related_objects)
        except Exception as exc:
            raise ImportErrorDetail(
                json.dumps({
                    'model': model_label,
                    'pk': primary_key,
                    'field': field.name,
                    'related_values': values,
                    'error': str(exc),
                }, default=str)
            ) from exc


def import_single_entry(entry):
    label = entry.get('model')
    model_class = MODEL_MAP.get(label)
    if model_class is None:
        raise ImportErrorDetail(f'Unsupported model label: {label}')

    pk = entry.get('pk')
    fields = entry.get('fields', {})
    attrs = {}
    m2m_payloads = {}
    for field_name, raw_value in fields.items():
        if field_name in {'id'} or field_name.endswith('_ptr'):
            continue
        field = model_class._meta.get_field(field_name)
        if isinstance(field, ManyToManyField):
            m2m_payloads[field] = raw_value
            continue
        if raw_value is None:
            attrs[field_name] = None
            continue
        attrs[field_name] = coerce_field_value(model_class, field_name, raw_value)

    instance = model_class.objects.filter(pk=pk).first()
    skipped = instance is not None
    if instance is None:
        instance = model_class(pk=pk, **attrs)
        instance.save(force_insert=True)

    apply_many_to_many_fields(instance, label, pk, m2m_payloads)

    return {'skipped': int(skipped), 'successful': int(not skipped), 'failed': 0}


def reset_sequences():
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        for model in [CustomUser, Category, Product, SupplierProduct, Transaction, Cart, CartItem, BankPaymentDetail]:
            sequence_name = f'{model._meta.db_table}_id_seq'
            cursor.execute(
                'SELECT setval(%s, COALESCE((SELECT MAX(id) FROM %s), 1), true)',
                [sequence_name, model._meta.db_table],
            )


def create_completion_marker(progress):
    DataImportMarker.objects.get_or_create(
        name='sqlite_to_postgres',
        defaults={
            'source_file': str(progress['data_file']),
            'total_objects': progress['total_importable'],
        },
    )


def process_batch(batch_size=BATCH_SIZE_DEFAULT):
    if batch_size <= 0 or batch_size > MAX_BATCH_SIZE:
        batch_size = BATCH_SIZE_DEFAULT

    progress = load_progress()
    if progress is None:
        raise ImportErrorDetail('No uploaded import file exists.')
    if progress['status'] == 'completed':
        return progress

    file_path = get_recorded_file_path(progress)
    diagnostics = get_file_diagnostics(file_path)
    logger.info(
        'Temporary import progress: data_file=%s file_exists=%s stage=%s batch=%s '
        'processed=%s successful=%s skipped=%s failed=%s batch_size=%s',
        diagnostics['file_path'],
        diagnostics['file_exists'],
        progress.get('stage_name'),
        progress.get('batch_number'),
        progress.get('processed'),
        progress.get('successful'),
        progress.get('skipped'),
        progress.get('failed'),
        batch_size,
    )
    if not file_path.is_file():
        raise ImportErrorDetail(
            'Uploaded import file is missing on the current instance filesystem. '
            + json.dumps(diagnostics)
        )

    if progress['status'] == 'uploaded':
        progress['status'] = 'processing'

    current_stage = progress['stage_index']
    batch_summary = None
    while current_stage < len(IMPORT_STAGES):
        stage_name, stage_labels = IMPORT_STAGES[current_stage]
        batch, stage_totals = collect_stage_batch(file_path, stage_labels, batch_size)
        progress['stage_name'] = stage_name

        if stage_totals == 0:
            current_stage += 1
            progress['stage_index'] = current_stage
            if current_stage >= len(IMPORT_STAGES):
                break
            continue

        if not batch:
            # All stage objects already exist, mark stage complete and continue.
            current_stage += 1
            progress['stage_index'] = current_stage
            continue

        failed_objects = []
        successful = 0
        skipped = 0
        with transaction.atomic():
            for entry in batch:
                label = entry.get('model')
                pk = entry.get('pk')
                try:
                    result = import_single_entry(entry)
                    skipped += result['skipped']
                    successful += result['successful']
                except ImportErrorDetail as exc:
                    failed_objects.append({'model': label, 'pk': pk, 'error': str(exc)})
                    break
                except Exception as exc:
                    failed_objects.append({'model': label, 'pk': pk, 'error': str(exc)})
                    break

            if failed_objects:
                raise ImportErrorDetail(json.dumps({'batch': progress['batch_number'] + 1, 'failed_objects': failed_objects}))

        progress['batch_number'] += 1
        progress['successful'] += successful
        progress['skipped'] += skipped
        progress['processed'] += successful + skipped
        progress['last_error'] = None
        progress['failed_objects'] = []
        progress['stage_index'] = current_stage
        progress['stage_name'] = stage_name
        save_progress(progress)
        logger.info(
            'Temporary import batch complete: data_file=%s stage=%s batch=%s '
            'processed=%s successful=%s skipped=%s failed=%s',
            str(file_path),
            stage_name,
            progress['batch_number'],
            progress['processed'],
            progress['successful'],
            progress['skipped'],
            progress['failed'],
        )
        return progress

    if current_stage >= len(IMPORT_STAGES):
        progress['status'] = 'completed'
        progress['completed_at'] = datetime.utcnow().isoformat() + 'Z'
        create_completion_marker(progress)
        reset_sequences()
        save_progress(progress)
        return progress

    save_progress(progress)
    return progress


def process_chunk_batch(stage_name, batch_size=BATCH_SIZE_DEFAULT):
    if batch_size <= 0 or batch_size > MAX_BATCH_SIZE:
        batch_size = BATCH_SIZE_DEFAULT
    progress = load_progress()
    if not progress or progress.get('mode') != 'chunks':
        raise ImportErrorDetail('No chunk upload state exists.')
    if stage_name not in dict(IMPORT_STAGES):
        raise ImportErrorDetail(f'Unknown import stage: {stage_name}')
    if progress['status'] == 'completed':
        return progress
    expected_stage = IMPORT_STAGES[progress['stage_index']][0] if progress['stage_index'] < len(IMPORT_STAGES) else None
    if stage_name != expected_stage:
        raise ImportErrorDetail(f'Expected stage {expected_stage!r}, received {stage_name!r}.')
    if not progress['stage_complete'].get(stage_name):
        raise ImportErrorDetail(f'Stage {stage_name} is not marked complete. Upload all chunks and mark the final chunk first.')

    stage_chunks = sorted(
        (name, record) for name, record in progress['chunks'].items() if record['stage'] == stage_name
    )
    while progress['current_chunk_index'] < len(stage_chunks):
        chunk_name, chunk_record = stage_chunks[progress['current_chunk_index']]
        file_path = Path(chunk_record['path']).resolve()
        progress['current_chunk'] = chunk_name
        diagnostics = get_file_diagnostics(file_path)
        logger.info('Temporary chunk progress: path=%s exists=%s stage=%s chunk=%s batch=%s offset=%s processed=%s successful=%s skipped=%s failed=%s batch_size=%s', diagnostics['file_path'], diagnostics['file_exists'], stage_name, chunk_name, progress['batch_number'], progress['chunk_object_offset'], progress['processed'], progress['successful'], progress['skipped'], progress['failed'], batch_size)
        if not file_path.is_file():
            raise ImportErrorDetail('Uploaded chunk is missing on the current instance filesystem. ' + json.dumps(diagnostics))

        batch = []
        for index, entry in enumerate(stream_json_objects(file_path)):
            if index < progress['chunk_object_offset']:
                continue
            if len(batch) >= batch_size:
                break
            batch.append(entry)

        if not batch:
            progress['current_chunk_index'] += 1
            progress['chunk_object_offset'] = 0
            progress['current_chunk'] = None
            save_progress(progress)
            continue

        failed_objects = []
        successful = 0
        skipped = 0
        with transaction.atomic():
            for entry in batch:
                try:
                    result = import_single_entry(entry)
                    skipped += result['skipped']
                    successful += result['successful']
                except Exception as exc:
                    failed_objects.append({'model': entry.get('model'), 'pk': entry.get('pk'), 'error': str(exc)})
                    break
            if failed_objects:
                raise ImportErrorDetail(json.dumps({'chunk': chunk_name, 'failed_objects': failed_objects}))

        progress['status'] = 'processing'
        progress['batch_number'] += 1
        progress['successful'] += successful
        progress['skipped'] += skipped
        progress['processed'] += successful + skipped
        progress['chunk_object_offset'] += len(batch)
        progress['last_error'] = None
        progress['failed_objects'] = []
        save_progress(progress)
        logger.info('Temporary chunk batch complete: stage=%s chunk=%s batch=%s processed=%s successful=%s skipped=%s failed=%s', stage_name, chunk_name, progress['batch_number'], progress['processed'], progress['successful'], progress['skipped'], progress['failed'])
        return progress

    progress['stage_index'] += 1
    progress['stage_name'] = IMPORT_STAGES[progress['stage_index']][0] if progress['stage_index'] < len(IMPORT_STAGES) else None
    progress['current_chunk_index'] = 0
    progress['chunk_object_offset'] = 0
    progress['current_chunk'] = None
    if progress['stage_index'] >= len(IMPORT_STAGES):
        progress['status'] = 'completed'
        progress['completed_at'] = datetime.utcnow().isoformat() + 'Z'
        create_completion_marker(progress)
        reset_sequences()
    save_progress(progress)
    return progress


def cleanup():
    progress = load_progress()
    if not progress or progress.get('status') != 'completed':
        raise ImportErrorDetail('Cleanup is allowed only after the complete import has finished.')
    removed = []
    file_paths = [Path(item['path']) for item in progress.get('chunks', {}).values()]
    file_paths.append(get_recorded_file_path(progress))
    for file_path in set(file_paths):
        if file_path.is_file():
            file_path.unlink()
            removed.append(str(file_path))
    if CHUNK_DIR.is_dir() and not any(CHUNK_DIR.iterdir()):
        CHUNK_DIR.rmdir()
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        removed.append(str(PROGRESS_FILE))
    return {
        'detail': 'Temporary upload and progress files were removed.',
        'removed': removed,
        **get_file_diagnostics(),
    }


def import_full_file(input_file, batch_size=BATCH_SIZE_DEFAULT):
    if not Path(input_file).exists():
        raise ImportErrorDetail('Import file not found.')
    if DataImportMarker.objects.filter(name='sqlite_to_postgres').exists():
        return get_status()

    initialize_upload(Path(input_file))
    status = None
    while True:
        status = process_batch(batch_size=batch_size)
        if status['status'] == 'completed':
            break
        if status['failed'] > 0 and status['last_error']:
            raise ImportErrorDetail(status['last_error'])
    return status
