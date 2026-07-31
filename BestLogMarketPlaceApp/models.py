from django.db import models
from django.contrib.auth.models import AbstractUser

class CustomUser(AbstractUser):
    first_name = models.CharField(max_length=20)
    last_name = models.CharField(max_length=20)
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=15, null=True, unique=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return f"{self.username} - {self.email}"

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'

class Category(models.Model):
    name = models.CharField(max_length=100, blank=True, null=True)
    slug = models.SlugField(max_length=120, blank=True, null=True, unique=True)
    supplier_category_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    description = models.TextField(blank=True, default="")
    image = models.ImageField(upload_to='category_images/', blank=True, null=True)
    order = models.PositiveIntegerField(default=0, help_text="Category display order")

    def __str__(self):
        return self.name or 'Unnamed Category'

    class Meta:
        verbose_name = 'Category'
        verbose_name_plural = 'Categories'
        ordering = ['order']  # This ensures categories are ordered by this field

class Product(models.Model):
    name = models.CharField(max_length=100, blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.TextField(blank=True, default="", null=True)
    view_link = models.URLField(max_length=300, blank=True, null=True)
    category = models.ForeignKey("Category", on_delete=models.CASCADE, related_name="products")
    account_details = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    supplier_product_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    supplier_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, default=0.00)
    supplier_name = models.CharField(max_length=200, blank=True, null=True)
    supplier_stock = models.PositiveIntegerField(default=0)
    supplier_synced_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.name} - {self.price}"

    def delete(self, *args, **kwargs):
        self.is_active = False
        self.save()

    def is_purchased(self):
        """Checks if this product is linked to an approved transaction."""
        return self.transactions.filter(transaction_status="approved").exists()

    is_purchased.boolean = True  # Shows as a boolean (Yes/No) in Django Admin
    is_purchased.short_description = "Purchased"  # Column name in Django Admin

    class Meta:
        verbose_name = "Product"
        verbose_name_plural = "Products"


class SupplierProduct(models.Model):
    """Supplier-specific product metadata kept separate from the storefront Product.

    - `product` links to the local storefront `Product` when a mapping is established.
    - `supplier_product_id` is stored as a string to support non-numeric IDs.
    - `raw_payload` stores the supplier's JSON payload for debugging/audit (redact secrets before showing).
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='supplier_products', null=True, blank=True)
    supplier_product_id = models.CharField(max_length=100, unique=True, db_index=True)
    supplier_category_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    supplier_name = models.CharField(max_length=200, blank=True, null=True)
    supplier_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, default=0.00)
    supplier_stock = models.IntegerField(default=0)
    supplier_synced_at = models.DateTimeField(blank=True, null=True)
    supplier_response = models.JSONField(blank=True, null=True, default=dict)
    delivered_keys_encrypted = models.TextField(blank=True, null=True)
    raw_payload = models.JSONField(blank=True, null=True, default=dict)

    def __str__(self):
        return f"{self.supplier_name or 'Supplier'} - {self.supplier_product_id}"

class Transaction(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='transactions', null=True, blank=True)  # Allow null values
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    products = models.ManyToManyField(Product, related_name='transactions')
    tx_ref = models.CharField(max_length=100, blank=True, null=True)
    credo_reference = models.CharField(max_length=100, blank=True, null=True)
    supplier_order_id = models.CharField(max_length=100, blank=True, null=True)
    supplier_product_id = models.PositiveIntegerField(blank=True, null=True, db_index=True)
    supplier_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, default=0.00)
    supplier_charge = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, default=0.00)
    delivered_keys = models.JSONField(blank=True, null=True, default=list)
    supplier_response = models.JSONField(blank=True, null=True, default=dict)
    DELIVERY_STATUS_CHOICES = (
        ('pending', 'pending'),
        ('delivered', 'delivered'),
        ('pending_supplier_delivery', 'pending supplier delivery'),
        ('failed', 'failed'),
    )
    delivery_status = models.CharField(max_length=40, choices=DELIVERY_STATUS_CHOICES, default='pending')
    delivered_keys_encrypted = models.TextField(blank=True, null=True)
    TRANSACTION_STATUS_CHOICES = (
        ('pending', 'pending'),
        ('processing', 'processing'),
        ('approved', 'approved'),
        ('declined', 'declined'),
    )
    transaction_status = models.CharField(max_length=20, choices=TRANSACTION_STATUS_CHOICES, default='pending')
    transaction_date = models.DateTimeField(auto_now_add=True)

    def set_delivered_keys(self, keys):
        """Store delivered keys encrypted if encryption key available, otherwise store as JSON in plain text.

        This method centralizes where keys are saved so we can change storage later.
        """
        import json, os
        from base64 import b64encode

        if not keys:
            self.delivered_keys_encrypted = None
            # keep legacy delivered_keys field empty to avoid dual sources
            self.delivered_keys = []
            return

        # If an encryption key is configured, use Fernet to encrypt the JSON blob.
        enc_key = os.getenv("FIELD_ENCRYPTION_KEY")
        payload = json.dumps(keys)
        if enc_key:
            try:
                from cryptography.fernet import Fernet
                f = Fernet(enc_key)
                token = f.encrypt(payload.encode('utf-8'))
                self.delivered_keys_encrypted = b64encode(token).decode('utf-8')
                # clear legacy field
                self.delivered_keys = []
                return
            except Exception:
                # Fall back to plain text if encryption fails
                pass

        # No encryption configured — store as JSON plain text in the new field
        self.delivered_keys_encrypted = payload
        self.delivered_keys = []

    def get_delivered_keys(self):
        """Return delivered keys by trying encrypted storage first, then legacy JSONField."""
        import json, os
        from base64 import b64decode

        if self.delivered_keys_encrypted:
            enc = self.delivered_keys_encrypted
            enc_key = os.getenv("FIELD_ENCRYPTION_KEY")
            # Try decryption if an encryption key is provided
            if enc_key:
                try:
                    from cryptography.fernet import Fernet
                    f = Fernet(enc_key)
                    token = b64decode(enc)
                    payload = f.decrypt(token).decode('utf-8')
                    return json.loads(payload)
                except Exception:
                    try:
                        # If value was stored as plain JSON string, attempt to parse directly
                        return json.loads(enc)
                    except Exception:
                        return []
            else:
                try:
                    return json.loads(enc)
                except Exception:
                    return []

        # Fallback to legacy delivered_keys JSONField
        return self.delivered_keys or []

    def __str__(self):
        return f"{self.user.username if self.user else 'Anonymous'} - {self.amount} - {self.transaction_status}"

    class Meta:
        verbose_name = 'Transaction'
        verbose_name_plural = 'Transactions'

class BankPaymentDetail(models.Model):
    account_number = models.CharField(max_length=100, blank=True, null=True)
    bank_name = models.CharField(max_length=100, blank=True, null=True)
    account_holder_name = models.CharField(max_length=100, blank=True, null=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.bank_name} - {self.account_number} - {self.account_holder_name} - {self.active}"

class Cart(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='cart', null=True, blank=True)
    session_key = models.CharField(max_length=40, null=True, blank=True)  # For anonymous users

    def __str__(self):
        if self.user:
            return f"Cart of {self.user.username}"
        else:
            return f"Cart for session {self.session_key}"

    def total_price(self):
        return sum(item.total_price() for item in self.items.all())


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        if self.cart and self.cart.user:
            return f"{self.quantity} of {self.product.name} in {self.cart.user.username}'s cart"
        return f"{self.quantity} of {self.product.name} in an anonymous cart"

    def total_price(self):
        return self.product.price * self.quantity


class DataImportMarker(models.Model):
    """Tracks whether the temporary SQLite-to-PostgreSQL import has already completed."""
    name = models.CharField(max_length=100, unique=True, default="sqlite_to_postgres")
    imported_at = models.DateTimeField(auto_now_add=True)
    source_file = models.CharField(max_length=255, blank=True, null=True)
    total_objects = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Data Import Marker"
        verbose_name_plural = "Data Import Markers"

