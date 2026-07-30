import os
import django
from django.test import Client
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'BestLogMarketPlaceProject.settings')
# Setup Django
# Ensure project root is on sys.path so imports work when running from any CWD
proj_root = os.path.dirname(os.path.dirname(__file__))
if proj_root not in sys.path:
    sys.path.insert(0, proj_root)

django.setup()

from BestLogMarketPlaceApp.models import Product, Cart, CartItem

c = Client()
print('GET / =>', c.get('/').status_code)
prods = list(Product.objects.all()[:5])
print('Sample products:', [(p.id, p.name, p.supplier_stock) for p in prods])
if not prods:
    print('No products to test.')
else:
    p1 = prods[0]
    p2 = prods[1] if len(prods) > 1 else prods[0]
    p1.description = 'Automated test description'
    p1.save()
    print('Updated description for', p1.id)
    # Add p1
    r = c.post(f'/add_to_cart/{p1.id}/')
    print('POST add p1 =>', r.status_code)
    # Add p2
    r = c.post(f'/add_to_cart/{p2.id}/')
    print('POST add p2 =>', r.status_code)
    # Add p1 again
    r = c.post(f'/add_to_cart/{p1.id}/')
    print('POST add p1 again =>', r.status_code)
    # Inspect cart
    cart_id = c.session.get('cart_id')
    cart = Cart.objects.filter(id=cart_id).first()
    print('Cart id:', cart_id)
    if cart:
        items = [(ci.product.id, ci.product.name, ci.quantity) for ci in cart.items.all()]
        print('Cart items after adds:', items)
    # Try to exceed stock for p1
    if p1.supplier_stock is not None:
        desired = (p1.supplier_stock or 0) + 2
        print('Attempting to set quantity to', desired, 'for product', p1.id)
        ci = CartItem.objects.filter(cart=cart, product=p1).first()
        if ci:
            r = c.post(f'/update_cart/{ci.id}/', {'quantity': desired})
            print('POST update_cart =>', r.status_code)
            cart.refresh_from_db()
            print('Post-update cart items:', [(x.product.id, x.quantity) for x in cart.items.all()])
        else:
            print('No cart item found for p1')
