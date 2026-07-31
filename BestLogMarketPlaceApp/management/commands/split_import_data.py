import json
from pathlib import Path

import ijson
from django.core.management.base import BaseCommand, CommandError


STAGES = {
    'users': {'BestLogMarketPlaceApp.customuser'},
    'categories': {'BestLogMarketPlaceApp.category'},
    'products': {'BestLogMarketPlaceApp.product'},
    'supplier_products': {'BestLogMarketPlaceApp.supplierproduct'},
    'bank_payment_details': {'BestLogMarketPlaceApp.bankpaymentdetail'},
    'transactions': {'BestLogMarketPlaceApp.transaction'},
    'carts': {'BestLogMarketPlaceApp.cart'},
    'cart_items': {'BestLogMarketPlaceApp.cartitem'},
}


class Command(BaseCommand):
    help = 'Split a Django JSON export into streaming, stage-specific import chunks.'

    def add_arguments(self, parser):
        parser.add_argument('input_file', type=Path)
        parser.add_argument('--output-dir', type=Path, default=Path('import_chunks'))
        parser.add_argument('--objects-per-chunk', type=int, default=250)

    def handle(self, *args, **options):
        input_file = options['input_file']
        output_dir = options['output_dir']
        objects_per_chunk = options['objects_per_chunk']
        if not input_file.is_file():
            raise CommandError(f'Input file does not exist: {input_file}')
        if objects_per_chunk < 1:
            raise CommandError('--objects-per-chunk must be at least 1')

        output_dir.mkdir(exist_ok=True, parents=True)
        handles = {}
        counts = {stage: 0 for stage in STAGES}
        chunk_numbers = {stage: 0 for stage in STAGES}
        chunk_counts = {stage: 0 for stage in STAGES}
        try:
            with input_file.open('rb') as source:
                for entry in ijson.items(source, 'item'):
                    model_label = entry.get('model') if isinstance(entry, dict) else None
                    stage = next((name for name, labels in STAGES.items() if model_label in labels), None)
                    if stage is None:
                        continue
                    if stage not in handles or counts[stage] % objects_per_chunk == 0:
                        if stage in handles:
                            handles[stage].write(']\n')
                            handles[stage].close()
                        chunk_numbers[stage] += 1
                        path = output_dir / f'{stage}-{chunk_numbers[stage]:04d}.json'
                        handle = path.open('w', encoding='utf-8')
                        handle.write('[\n')
                        handles[stage] = handle
                        chunk_counts[stage] += 1
                    if counts[stage] % objects_per_chunk:
                        handles[stage].write(',\n')
                    json.dump(entry, handles[stage], ensure_ascii=False, separators=(',', ':'))
                    counts[stage] += 1
        finally:
            for handle in handles.values():
                handle.write(']\n')
                handle.close()

        total = sum(counts.values())
        self.stdout.write(self.style.SUCCESS(f'Created {sum(chunk_counts.values())} chunks with {total} importable objects.'))
        for stage in STAGES:
            self.stdout.write(f'{stage}: {counts[stage]} objects in {chunk_counts[stage]} chunks')
