from django.core.management.base import BaseCommand
from BestLogMarketPlaceApp.models import Product, SupplierProduct, Category

class Command(BaseCommand):
    help = 'Inspect supplier and product tables and print counts and samples'

    def handle(self, *args, **options):
        print('Products:', Product.objects.count())
        print('SupplierProducts:', SupplierProduct.objects.count())
        print('Categories:', Category.objects.count())
        print('\nSample Products:')
        for p in Product.objects.all()[:20]:
            print(p.id, p.name, 'price=', p.price, 'is_active=', p.is_active, 'supplier_product_id=', p.supplier_product_id, 'supplier_stock=', p.supplier_stock)
        print('\nSample SupplierProducts:')
        for s in SupplierProduct.objects.all()[:20]:
            print(s.id, s.supplier_product_id, s.supplier_name, 'supplier_price=', s.supplier_price, 'stock=', s.supplier_stock, 'product_id=', s.product.id if s.product else None)
