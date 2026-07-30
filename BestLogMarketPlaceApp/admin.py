# admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.html import format_html
from django.urls import reverse
from .models import CustomUser, Category, Product, Transaction,BankPaymentDetail,Cart,CartItem
from .services.emonbestlogs import EmonBestLogsAPIError, EmonBestLogsService
from .models import SupplierProduct


class CustomUserAdmin(UserAdmin):
    model = CustomUser
    list_display = ('username', 'email', 'first_name', 'last_name', 'phone_number', 'is_staff')
    fieldsets = UserAdmin.fieldsets + (
        (None, {'fields': ('phone_number',)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        (None, {'fields': ('phone_number',)}),
    )

class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'order', 'description')  # Display the order field and description
    list_editable = ('order',)         # Make the order field editable directly from the list view
    ordering = ('order',)              # Sort the categories by the 'order' field in the admin
    fields = ('name', 'slug', 'supplier_category_id', 'description', 'image', 'order')
    readonly_fields = ('slug', 'supplier_category_id')

class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "supplier_price", "supplier_stock", "category", "is_purchased", "view_link")
    list_filter = ("category", "is_active")
    search_fields = ("name", "category__name", "supplier_product_id")
    fields = ("name", "price", "description", "view_link", "category", "is_active", "supplier_product_id", "supplier_price", "supplier_stock", "supplier_name")

    def is_purchased(self, obj):
        return obj.is_purchased()

    is_purchased.boolean = True  # Display as a boolean
    is_purchased.short_description = "Purchased"


class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'get_products', 'supplier_order_id', 'supplier_charge', 'delivery_status', 'transaction_status', 'transaction_date')
    list_filter = ('transaction_status', 'delivery_status')
    search_fields = ('user__username', 'supplier_order_id', 'tx_ref')
    actions = ['approve_transactions', 'decline_transactions', 'retry_supplier_delivery']

    def get_products(self, obj):
        links = []
        for product in obj.products.all():
            url = reverse('admin:BestLogMarketPlaceApp_product_change', args=[product.id])
            links.append(f'<a href="{url}">{product.name}</a>')
        return format_html(', '.join(links))
    
    get_products.short_description = 'Products'
    get_products.allow_tags = True

    def approve_transactions(self, request, queryset):
        queryset.update(transaction_status='approved')
        for transaction in queryset:
            for product in transaction.products.all():
                product.delete()

    approve_transactions.short_description = 'Approve selected transactions'

    def decline_transactions(self, request, queryset):
        queryset.update(transaction_status='declined')

    decline_transactions.short_description = 'Decline selected transactions'

    def retry_supplier_delivery(self, request, queryset):
        for transaction in queryset:
            # Only allow retry for transactions that are approved but not already delivered
            if transaction.transaction_status != 'approved':
                continue
            if transaction.delivery_status == 'delivered' or (transaction.get_delivered_keys() and len(transaction.get_delivered_keys()) > 0):
                continue
            try:
                service = EmonBestLogsService()
                order_numbers = []
                key_list = []
                total_charge = 0.0
                for product in transaction.products.all():
                    if not product.supplier_product_id:
                        raise AttributeError(f"Missing supplier product ID for {product.name}")
                    # prefer SupplierProduct mapping
                    supplier_record = SupplierProduct.objects.filter(product=product).first()
                    supplier_id = supplier_record.supplier_product_id if supplier_record and supplier_record.supplier_product_id else product.supplier_product_id
                    idempotency_key = f"admin-retry-txn-{transaction.pk}-prod-{product.id}-{transaction.tx_ref or transaction.pk}"
                    delivery = service.buy_product(supplier_id, quantity=1, idempotency_key=idempotency_key)
                    if delivery.get('order_id'):
                        order_numbers.append(str(delivery.get('order_id')))
                    key_list.extend(delivery.get('keys', []))
                    total_charge += float(delivery.get('charge', 0.00) or 0.00)
                transaction.delivery_status = 'delivered'
                transaction.supplier_order_id = ', '.join(order_numbers) if order_numbers else None
                transaction.supplier_charge = total_charge
                transaction.delivered_keys = key_list
                transaction.supplier_response = {'deliveries': key_list}
                transaction.save()
            except (EmonBestLogsAPIError, AttributeError) as exc:
                transaction.delivery_status = 'pending_supplier_delivery'
                transaction.supplier_response = {'error': str(exc)}
                transaction.save(update_fields=['delivery_status', 'supplier_response'])

    retry_supplier_delivery.short_description = 'Retry supplier delivery for selected approved transactions'

admin.site.register(Transaction, TransactionAdmin)
admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(Product, ProductAdmin)


class SupplierProductAdmin(admin.ModelAdmin):
    list_display = ("supplier_product_id", "supplier_name", "supplier_price", "supplier_stock", "product")
    search_fields = ("supplier_product_id", "supplier_name")


admin.site.register(SupplierProduct, SupplierProductAdmin)
admin.site.register(BankPaymentDetail)
admin.site.register(Cart)
admin.site.register(CartItem)
