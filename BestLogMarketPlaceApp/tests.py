from django.test import TestCase
from django.core.management import call_command
from unittest.mock import patch
from BestLogMarketPlaceApp.models import Product, SupplierProduct, Category

# Create your tests here.


class SyncCommandTests(TestCase):
    @patch('BestLogMarketPlaceApp.management.commands.sync_supplier_products.EmonBestLogsService')
    def test_sync_creates_supplierproduct_and_product_when_payload_valid(self, mock_service_class):
        mock_service = mock_service_class.return_value
        mock_service.get_categories.return_value = [
            {"id": 10, "name": "Utilities"},
        ]
        mock_service.get_products.return_value = [
            {
                "id": 123,
                "name": "Supplier Item",
                "price": "50.00",
                "my_selling_price": "75.00",
                "category_id": 10,
                "stock": 10,
                "description": "Test account details",
                "view_link": "https://supplier.example/item/123",
                "active": True,
            }
        ]

        call_command('sync_supplier_products')

        cat = Category.objects.filter(name='Utilities').first()
        self.assertIsNotNone(cat)
        product = Product.objects.filter(name='Supplier Item').first()
        self.assertIsNotNone(product)
        self.assertTrue(product.is_active)
        supplier = SupplierProduct.objects.filter(supplier_product_id='123').first()
        self.assertIsNotNone(supplier)
        self.assertEqual(supplier.supplier_product_id, '123')

    @patch('BestLogMarketPlaceApp.management.commands.sync_supplier_products.EmonBestLogsService')
    def test_sync_marks_out_of_stock_products_unavailable(self, mock_service_class):
        mock_service = mock_service_class.return_value
        mock_service.get_categories.return_value = [
            {"id": 20, "name": "Games"},
        ]
        mock_service.get_products.return_value = [
            {
                "id": 222,
                "name": "Out of Stock Item",
                "price": "40.00",
                "my_selling_price": "60.00",
                "category_id": 20,
                "stock": 0,
                "active": True,
            }
        ]

        call_command('sync_supplier_products')

        product = Product.objects.filter(supplier_product_id='222').first()
        self.assertIsNotNone(product)
        self.assertFalse(product.is_active)

    @patch('BestLogMarketPlaceApp.management.commands.sync_supplier_products.EmonBestLogsService')
    def test_sync_restores_product_availability_when_stock_returns(self, mock_service_class):
        mock_service = mock_service_class.return_value
        mock_service.get_categories.return_value = [
            {"id": 30, "name": "Tools"},
        ]
        mock_service.get_products.return_value = [
            {
                "id": 333,
                "name": "Back In Stock",
                "price": "25.00",
                "my_selling_price": "35.00",
                "category_id": 30,
                "stock": 0,
                "active": False,
            }
        ]
        call_command('sync_supplier_products')

        product = Product.objects.filter(supplier_product_id='333').first()
        self.assertIsNotNone(product)
        self.assertFalse(product.is_active)

        mock_service.get_products.return_value = [
            {
                "id": 333,
                "name": "Back In Stock",
                "price": "25.00",
                "my_selling_price": "35.00",
                "category_id": 30,
                "stock": 5,
                "active": True,
            }
        ]
        call_command('sync_supplier_products')
        product.refresh_from_db()
        self.assertTrue(product.is_active)

    @patch('BestLogMarketPlaceApp.management.commands.sync_supplier_products.EmonBestLogsService')
    def test_sync_updates_existing_supplier_and_product(self, mock_service_class):
        mock_service = mock_service_class.return_value
        mock_service.get_categories.return_value = [
            {"id": 40, "name": "CatA"},
        ]
        mock_service.get_products.return_value = [
            {"id": 333, "name": "Orig", "price": "20.00", "my_selling_price": "30.00", "category_id": 40, "stock": 5}
        ]
        call_command('sync_supplier_products')
        prod = Product.objects.filter(name='Orig').first()
        self.assertIsNotNone(prod)

        mock_service.get_products.return_value = [
            {"id": 333, "name": "Orig Updated", "price": "25.00", "my_selling_price": "35.00", "category_id": 40, "stock": 8}
        ]
        call_command('sync_supplier_products')
        supplier = SupplierProduct.objects.filter(supplier_product_id='333').first()
        self.assertIsNotNone(supplier)
        self.assertEqual(int(supplier.supplier_stock), 8)
