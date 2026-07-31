import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib import messages
from .forms import RegisterForm,ProfileForm
from .models import Category, Product,Transaction,BankPaymentDetail,Cart, CartItem, CustomUser, DataImportMarker
from django.db.models import Sum
from django.db.models import Count, Q
from django.contrib.sessions.models import Session
from django.core.mail import send_mail
from django.template.loader import render_to_string
import requests
from django.conf import settings
from django.core.management import call_command
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services.emonbestlogs import EmonBestLogsAPIError, EmonBestLogsService
from .models import SupplierProduct
from django.db import transaction as db_transaction

logger = logging.getLogger(__name__)
TEMP_IMPORT_DIR = Path(settings.BASE_DIR) / '.tmp_imports'
TEMP_IMPORT_DIR.mkdir(exist_ok=True, parents=True)
MAX_IMPORT_FILE_SIZE = 50 * 1024 * 1024


# TEMPORARY DATA MIGRATION ENDPOINT - REMOVE AFTER SUCCESSFUL IMPORT

def _require_data_import_token(request):
    token = os.getenv('DATA_IMPORT_TOKEN')
    if not token:
        return None, JsonResponse({'detail': 'Data import token is not configured.'}, status=503)

    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None, JsonResponse({'detail': 'Missing bearer token.'}, status=403)

    provided_token = auth_header[len('Bearer '):].strip()
    if not hmac.compare_digest(provided_token, token):
        return None, JsonResponse({'detail': 'Invalid token.'}, status=403)

    return token, None


@csrf_exempt
@require_POST
def temporary_data_import_upload(request):
    token, error_response = _require_data_import_token(request)
    if error_response is not None:
        return error_response

    uploaded_file = request.FILES.get('file')
    if uploaded_file is None or uploaded_file.name != 'data.json':
        return JsonResponse({'detail': 'Please upload a file named data.json.'}, status=400)

    if uploaded_file.size > MAX_IMPORT_FILE_SIZE:
        return JsonResponse({'detail': 'Uploaded file exceeds the maximum supported size.'}, status=413)

    upload_path = TEMP_IMPORT_DIR / 'data.json'
    with upload_path.open('wb') as handle:
        for chunk in uploaded_file.chunks():
            handle.write(chunk)

    return JsonResponse({'detail': 'Upload complete.', 'stored_at': str(upload_path)})


@csrf_exempt
@require_POST
def temporary_data_import_run(request):
    token, error_response = _require_data_import_token(request)
    if error_response is not None:
        return error_response

    if DataImportMarker.objects.filter(name='sqlite_to_postgres').exists():
        return JsonResponse({'detail': 'Data import has already been completed.'}, status=200)

    upload_path = TEMP_IMPORT_DIR / 'data.json'
    if not upload_path.exists():
        return JsonResponse({'detail': 'No uploaded data.json was found.'}, status=404)

    try:
        call_command('import_sqlite_data', input_file=str(upload_path), verbosity=1)
    except Exception as exc:
        logger.exception('Temporary data import failed')
        return JsonResponse({'detail': f'Import failed: {exc}'}, status=400)

    return JsonResponse({'detail': 'Data import completed successfully.'}, status=200)


def maintain(request):
    return render(request,'BestLogMarketPlaceApp/maintain.html')

from django.db.models import Count, Q

def home(request):
    # Get all categories with annotated product counts excluding approved transactions
    cart_item_count = get_cart_item_count(request)
    categories = Category.objects.prefetch_related(
        'products',
        'products__transactions'
    ).annotate(
        total_products=Count('products', filter=~Q(products__transactions__transaction_status='approved'))
    ).order_by('-order')  # Order by the 'order' field in descending order

    # Optionally filter categories based on a query parameter
    q = request.GET.get('q', '')
    if q and q != 'all':
        categories = categories.filter(
            Q(name__icontains=q) | Q(products__name__icontains=q)
        )

    no_categories_found = not categories.exists()

    # Adding the filtering logic for products directly
    for category in categories:
        category.filtered_products = category.products.exclude(transactions__transaction_status='approved')

    cart = get_or_create_cart(request)
    cart_product_ids = cart.items.values_list('product_id', flat=True)
    context = {
        'categories': categories,
        'q': q,
        'no_categories_found': no_categories_found,
        'cart_item_count': cart_item_count,
        'cart': cart,
        'cart_product_ids': cart_product_ids,
        'cart_items_map': {ci.product_id: {'id': ci.id, 'quantity': ci.quantity} for ci in cart.items.all()},
    }
    return render(request, 'BestLogMarketPlaceApp/index.html', context)

def product_detail(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    cart = get_or_create_cart(request)
    cart_product_ids = cart.items.values_list('product_id', flat=True)
    context = {
        'product': product,
        'cart': cart,
        'cart_product_ids': cart_product_ids,
    }
    return render(request, 'BestLogMarketPlaceApp/product_detail.html', context)

def category_products_view(request, category_id):
    # Fetch the specific category or return 404 if not found
    category = get_object_or_404(Category, id=category_id)

    # Fetch products that are not linked to an "approved" transaction
    products = category.products.filter(
        ~Q(transactions__transaction_status='approved')
    ).distinct()
    cart = get_or_create_cart(request)
    # Get all product IDs in the cart
    cart_product_ids = cart.items.values_list('product_id', flat=True)
    # Pass the category and filtered products to the template
    context = {
        'category': category,
        'products': products,
        'cart': cart,
        'cart_product_ids': cart_product_ids,
        'cart_items_map': {ci.product_id: {'id': ci.id, 'quantity': ci.quantity} for ci in cart.items.all()},
    }
    return render(request, 'BestLogMarketPlaceApp/category_products.html', context)

def get_or_create_cart(request):
    if request.user.is_authenticated:
        cart, created = Cart.objects.get_or_create(user=request.user)
    else:
        # Session-based cart for guest users
        cart_id = request.session.get('cart_id')
        if cart_id:
            cart = Cart.objects.filter(id=cart_id).first()
            if not cart:
                cart = Cart.objects.create()  # Create a new Cart object
                request.session['cart_id'] = cart.id
        else:
            cart = Cart.objects.create()  # Create new Cart object
            request.session['cart_id'] = cart.id
    return cart

def get_cart_item_count(request):
    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user).first()
    else:
        cart_id = request.session.get('cart_id')
        cart = Cart.objects.filter(id=cart_id).first() if cart_id else None

        # Use session-based cart item count if it's stored
        if cart and 'cart_item_count' not in request.session:
            request.session['cart_item_count'] = cart.items.count()

    return request.session.get('cart_item_count', 0) if not request.user.is_authenticated else cart.items.count() if cart else 0




def add_to_cart(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    cart = get_or_create_cart(request)
    # Determine desired quantity (increment by 1) without creating the CartItem first
    existing_item = CartItem.objects.filter(cart=cart, product=product).first()
    desired_quantity = 1 if not existing_item else existing_item.quantity + 1

    # Backend stock validation
    available = product.supplier_stock or 0
    if desired_quantity > available:
        messages.error(request, f'Only {available} units are currently available.')
        return redirect(request.META.get('HTTP_REFERER', 'home'))

    # Create or update the cart item after validation
    if existing_item:
        existing_item.quantity = desired_quantity
        existing_item.save()
        cart_item = existing_item
    else:
        cart_item = CartItem.objects.create(cart=cart, product=product, quantity=desired_quantity)

    # Update the session cart item count for non-authenticated users
    if not request.user.is_authenticated:
        request.session['cart_item_count'] = cart.items.count()  # Update session with the correct count

    # Display success message and redirect
    messages.success(request, f'Added {product.name} to cart.')
    return redirect(request.META.get('HTTP_REFERER', 'home'))

def view_cart(request):
    cart = get_or_create_cart(request)
    return render(request, 'BestLogMarketPlaceApp/cart.html', {'cart': cart})


def update_cart(request, item_id):
    """Update quantity for a cart item. Validates stock on the backend."""
    if request.method != 'POST':
        return redirect('view_cart')

    cart = get_or_create_cart(request)
    cart_item = get_object_or_404(CartItem, id=item_id, cart=cart)
    try:
        qty = int(request.POST.get('quantity', cart_item.quantity))
    except (TypeError, ValueError):
        messages.error(request, 'Invalid quantity.')
        return redirect('view_cart')

    if qty < 1:
        messages.error(request, 'Quantity must be at least 1.')
        return redirect('view_cart')

    product = cart_item.product
    available = product.supplier_stock or 0
    if qty > available:
        messages.error(request, f'Only {available} units are currently available.')
        return redirect('view_cart')

    cart_item.quantity = qty
    cart_item.save()
    messages.success(request, 'Cart updated.')
    return redirect('view_cart')

def remove_from_cart(request, item_id):
    cart = get_or_create_cart(request)
    cart_item = get_object_or_404(CartItem, id=item_id, cart=cart)
    cart_item.delete()
    messages.success(request, 'Item removed from cart.')
    return redirect('view_cart')


def about(request):
    return render(request, 'BestLogMarketPlaceApp/p_about.html')


def contact(request):
    return render(request, 'BestLogMarketPlaceApp/contact.html')

@login_required(login_url='/home')
def dashboard(request):
    orders = Transaction.objects.filter(user=request.user, transaction_status='approved').prefetch_related('products')
    order_count = orders.count()
    total_amount_paid = orders.aggregate(Sum('amount'))['amount__sum'] or 0.0
    context = {
        'orders': orders,
        'order_count': order_count,
        'total_amount_paid': total_amount_paid,
    }
    return render(request, 'BestLogMarketPlaceApp/Dashboard.html', context)


def register(request):
    if request.user.is_authenticated:
        return redirect('home')
    else:
        if request.method == 'POST':
            form = RegisterForm(request.POST)
            if form.is_valid():
                user = form.save(commit=False)
                user.save()  # Save the user before authenticating
                login(request, user)
                messages.success(request, f'Account created for {user.username}!')
                return redirect('home')
            else:
                messages.error(request, 'Error creating account. Please check the form.')
        else:
            form = RegisterForm()
    return render(request, 'BestLogMarketPlaceApp/register.html', {'form': form})

def login_user(request):
    if request.user.is_authenticated:
        return redirect('home')
    else:
        error_message = None
        if request.method == 'POST':
            email = request.POST['email']
            password = request.POST['password']
            user = authenticate(request, email=email, password=password)

            if user is not None:
                login(request, user)
                return redirect('home')
            else:
                messages.error(request, 'Invalid email or password.')
        return render(request, 'BestLogMarketPlaceApp/login.html', {'error_message': error_message})


def logout_user(request):
    logout(request)
    return redirect('home')

def profile_page(request):
    # if request.method == 'POST':
    #     form = ProfileForm(request.POST, request.FILES, instance=request.user)
    #     if form.is_valid():
    #         form.save()
#         return redirect('dashboard')
    # else:
    #     form = ProfileForm(instance=request.user)
    # return render(request, 'CoinacadeApp/dashboard/profile_page.html', {'form': form})
    return render(request, 'BestLogMarketPlaceApp/profile-setting.html')

import uuid
def payment_page(request):
    cart = get_or_create_cart(request)

    if request.method == "POST":
        customer_email = request.POST.get("customer[email]")
        customer_first_name = request.POST.get("customer[first_name]")
        customer_last_name = request.POST.get("customer[last_name]")
        customer_phone = request.POST.get("customer[phone]") or ""
        request.session["customer_email"] = customer_email
        request.session["customer_first_name"] = customer_first_name
        request.session["customer_last_name"] = customer_last_name

        if request.user.is_authenticated:
            user = request.user
        else:
            user = CustomUser.objects.filter(email=customer_email).first()
            if not user:
                username = f"guest_{customer_email.split('@')[0]}_{uuid.uuid4().hex[:8]}"
                user = CustomUser.objects.create_user(
                    username=username,
                    email=customer_email,
                    first_name=customer_first_name if customer_first_name else "Guest",
                    last_name=customer_last_name if customer_last_name else "",
                    password=uuid.uuid4().hex
                )

        # Ensure the total price is a float
        total_price = float(cart.total_price())

        # Validate that the total price is within the allowed range
        if total_price < 50.00 or total_price > 1000000.00:
            messages.error(request, "The total amount must be between NGN 50.00 and NGN 1,000,000.00. Please adjust your cart.")
            return redirect("payment_page")

        # Create a unique transaction reference
        tx_ref = f"TXN_{uuid.uuid4().hex}"
        transaction = Transaction.objects.create(
            user=user,
            amount=total_price,
            transaction_status="pending",
            tx_ref=tx_ref
        )

        # Add products from the cart to the transaction and record originating cart id
        for item in cart.items.all():
            transaction.products.add(item.product)
        # Store cart id on transaction.supplier_response so delivery can know quantities
        transaction.supplier_response = transaction.supplier_response or {}
        transaction.supplier_response.update({"cart_id": str(cart.id)})
        transaction.save()


        # Ensure customer phone number starts with +234
        if customer_phone and not customer_phone.startswith("+234"):
            customer_phone = f"+234{customer_phone.lstrip('0')}"

        # Convert amount to kobo by multiplying by 100
        amount_in_kobo = int(total_price * 100)

        payload = {
            "amount": amount_in_kobo,  # Send amount as an integer in kobo
            "bearer": 0,
            "callbackUrl": settings.CREDO_CALLBACK_URL,
            "channels": ["card", "bank"],
            "currency": "NGN",
            "customerFirstName": customer_first_name or "Guest",
            "customerLastName": customer_last_name or "",
            "customerPhoneNumber": customer_phone,
            "email": customer_email,
            "reference": tx_ref,
            "metadata": {
                "cart_id": str(cart.id),
                "customFields": [
                    {"variable_name": "source", "value": "BestLog Marketplace", "display_name": "Source"}
                ]
            }
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": settings.CREDO_PUBLIC_KEY
        }

        try:
            # Send the payment request to Credo
            response = requests.post(
                f"{settings.CREDO_BASE_URL}/transaction/initialize",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            payment_data = response.json()

            if payment_data.get("status") == 200 and payment_data.get("data", {}).get("authorizationUrl"):
                transaction.credo_reference = payment_data["data"].get("credoReference")
                transaction.save()
                return redirect(payment_data["data"]["authorizationUrl"])
            else:
                messages.error(request, payment_data.get("message", "Failed to initiate payment. Please try again."))
                return redirect("payment_page")

        except requests.RequestException as e:
            if e.response is not None:
                error_detail = e.response.text
                try:
                    error_json = e.response.json()
                    error_message = error_json.get("message", str(e))
                    error_details = error_json.get("error", [])
                    full_error = f"{error_message}: {json.dumps(error_details)}"
                except ValueError:
                    full_error = str(e)
            else:
                full_error = str(e)
            messages.error(request, f"Payment initiation failed: {full_error}")
            return redirect("payment_page")

    return render(request, "BestLogMarketPlaceApp/payment_page.html", {"cart": cart})


def _deliver_transaction_with_supplier(transaction):
    # Use a DB lock to avoid concurrent deliveries for the same transaction.
    try:
        with db_transaction.atomic():
            txn = (
                type(transaction).objects.select_for_update()
                .filter(pk=transaction.pk)
                .first()
            )
            # If already delivered, short-circuit
            if txn.delivered_keys:
                txn.delivery_status = "delivered"
                txn.save(update_fields=["delivery_status"])
                return txn

            service = EmonBestLogsService()

            delivery_items = []
            order_numbers = []
            key_list = []
            total_charge = 0.0

            # Attempt to read originating cart id (stored earlier on transaction.supplier_response)
            source_items = []
            cart_id = None
            try:
                if txn.supplier_response and isinstance(txn.supplier_response, dict):
                    cart_id = txn.supplier_response.get('cart_id')
            except Exception:
                cart_id = None

            if cart_id:
                cart_obj = Cart.objects.filter(id=cart_id).first()
                if cart_obj:
                    for itm in cart_obj.items.all():
                        source_items.append({'product': itm.product, 'quantity': itm.quantity})

            # Fallback: if no cart found, use transaction.products (quantity=1)
            if not source_items:
                for p in txn.products.all():
                    source_items.append({'product': p, 'quantity': 1})

            for entry in source_items:
                product = entry['product']
                qty = int(entry.get('quantity', 1) or 1)

                # Prefer SupplierProduct mapping when available
                supplier_record = SupplierProduct.objects.filter(product=product).first()
                supplier_id = None
                if supplier_record and supplier_record.supplier_product_id:
                    supplier_id = supplier_record.supplier_product_id
                elif product.supplier_product_id:
                    supplier_id = product.supplier_product_id

                if not supplier_id:
                    raise EmonBestLogsAPIError(f"Missing supplier product ID for product: {product.name}")

                # Build an idempotency key to avoid duplicate buys on retries
                idempotency_key = f"txn-{txn.pk}-prod-{product.id}-{txn.tx_ref or txn.pk}"

                supplier_payload = service.buy_product(supplier_id, quantity=qty, idempotency_key=idempotency_key)
                keys = supplier_payload.get("keys") or []
                charge = supplier_payload.get("charge") or 0.00
                order_number = supplier_payload.get("order_id")

                delivery_items.append({
                    "product_id": product.id,
                    "product_name": product.name,
                    "supplier_product_id": supplier_id,
                    "supplier_order_id": order_number,
                    "keys": keys,
                    "charge": charge,
                    "quantity": qty,
                })
                if order_number:
                    order_numbers.append(str(order_number))
                if keys:
                    key_list.extend(keys)
                total_charge += float(charge or 0.0)

            txn.supplier_order_id = ", ".join(order_numbers) if order_numbers else None
            txn.supplier_charge = total_charge
            txn.set_delivered_keys(key_list)
            txn.supplier_response = {"deliveries": delivery_items, "delivery_status": "delivered"}
            txn.delivery_status = "delivered"
            txn.transaction_status = "approved"
            txn.save()
            return txn
    except EmonBestLogsAPIError as exc:
        logger.exception("Supplier delivery failed for transaction %s", transaction.id)
        transaction.delivery_status = "pending_supplier_delivery"
        transaction.supplier_response = {"error": str(exc)}
        transaction.save(update_fields=["delivery_status", "supplier_response"])
        return transaction


# Updated pending view with verification
def pending(request):
    tx_ref = request.GET.get("reference")  # Your reference from initialization
    credo_ref = request.GET.get("credoReference")  # Credo's reference
    customer_email = request.session.get("customer_email")

    cart = get_or_create_cart(request)
    transaction = Transaction.objects.filter(tx_ref=tx_ref).first()

    if not transaction:
        messages.error(request, "Transaction not found.")
        return redirect("home")

    # Verify payment with Credo Central
    headers = {
        "Accept": "application/json",
        "Authorization": settings.CREDO_SECRET_KEY  # Use secret key for verification
    }
    try:
        response = requests.get(
            f"{settings.CREDO_BASE_URL}/transaction/{tx_ref}/verify",
            headers=headers
        )
        response.raise_for_status()
        verification_data = response.json()

        # Check transaction status
        if verification_data.get("status") == 200 and verification_data.get("data", {}).get("status") == 0:
            # Mark payment verified and move to processing so delivery is attempted atomically.
            transaction.transaction_status = "processing"
            transaction.credo_reference = verification_data["data"].get("transRef", credo_ref)
            transaction.save()

            transaction = _deliver_transaction_with_supplier(transaction)

            if customer_email:
                subject = "Your Purchase Details"
                key_details = "\n".join([f"- {key}" for key in (transaction.get_delivered_keys() or [])]) or "Your supplier delivery is being processed."
                product_details = "\n".join([f"{product.name}: {product.account_details or 'Digital Product'}" for product in transaction.products.all()])
                message = (
                    "Thank you for your purchase. Here are the product details:\n\n"
                    f"{product_details}\n\n"
                    "Your delivered keys:\n"
                    f"{key_details}\n\n"
                    "Best regards,\nBestLog Marketplace Team"
                )
                send_mail(subject, message, "bestlogsmarketplace@gmail.com", [customer_email])
            cart.items.all().delete()
        else:
            transaction.transaction_status = "declined"
            transaction.save()
            messages.error(request, verification_data.get("message", "Payment declined."))

    except requests.RequestException as e:
        messages.error(request, f"Payment verification failed: {str(e)}")
        transaction.transaction_status = "declined"
        transaction.save()

    return render(request, "BestLogMarketPlaceApp/pending.html", {
        "transaction_status": transaction.transaction_status,
        "transaction": transaction,
        "credo_reference": credo_ref,
        "payment_status": transaction.transaction_status,
    })


@login_required(login_url='/login_user')
def payment_history(request):
    transactions = Transaction.objects.filter(user=request.user)
    context = {'transactions': transactions}
    return render(request, 'BestLogMarketPlaceApp/payment_history.html', context)

@login_required(login_url='/login_user')
def orders(request):
    orders = Transaction.objects.filter(user=request.user, transaction_status='approved')
    context = {'orders': orders}
    return render(request, "BestLogMarketPlaceApp/orders.html", context)

@login_required(login_url='/login_user')
def order_detail(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    transaction = Transaction.objects.filter(user=request.user, products__id=product_id).order_by('-transaction_date').first()
    context = {
        'product': product,
        'transaction': transaction,
        'delivered_keys': transaction.delivered_keys if transaction else [],
    }
    return render(request,"BestLogMarketPlaceApp/order_detail.html", context)

def profile_page(request):
    if request.method == 'POST':
        form = ProfileForm(request.POST,instance=request.user)
        if form.is_valid():
            form.save()
            return redirect('dashboard')
    else:
        form = ProfileForm(instance=request.user)
    return render(request, 'BestLogMarketPlaceApp/profile-setting.html', {'form': form})

def privacy_page(request):
    return render (request,'BestLogMarketPlaceApp/privacy.html' )

def terms_page(request):
    return render (request,'BestLogMarketPlaceApp/terms_page.html' )

def rules_page(request):
    return render (request,'BestLogMarketPlaceApp/rules.html' )


from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum, Count, Max
from django.db.models.functions import TruncWeek, TruncMonth
from django.utils import timezone
from datetime import timedelta

try:
    from chartjs.views.lines import BaseLineChartView
except ImportError:
    class BaseLineChartView:
        pass

from django.views.generic import TemplateView


# Create a chart view class
class RevenueChartView(BaseLineChartView):
    def get_labels(self):
        # Get the last 7 weeks
        dates = []
        for i in range(7):
            week = timezone.now() - timedelta(weeks=i)
            dates.append(week.strftime('%Y-%m-%d'))
        return dates

    def get_data(self):
        # Get weekly revenue data
        weekly_data = []
        for i in range(7):
            week_start = timezone.now() - timedelta(weeks=i)
            week_revenue = Transaction.objects.filter(
                transaction_status='approved',
                transaction_date__week=week_start.isocalendar()[1]
            ).aggregate(total=Sum('amount'))['total'] or 0
            weekly_data.append(float(week_revenue))
        return [weekly_data]

    def get_providers(self):
        return ["Revenue"]

@staff_member_required
def custom_admin_dashboard(request):
    # Get current date and date 30 days ago
    today = timezone.now()
    thirty_days_ago = today - timedelta(days=30)
    t_products = Product.objects.all()
    t_users = CustomUser.objects.all()

    # Total revenue
    total_revenue = Transaction.objects.filter(
        transaction_status='approved'
    ).aggregate(total=Sum('amount'))['total'] or 0

    # Revenue in last 30 days
    recent_revenue = Transaction.objects.filter(
        transaction_status='approved',
        transaction_date__gte=thirty_days_ago
    ).aggregate(total=Sum('amount'))['total'] or 0

    # Get current date and time
    today = timezone.now()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    # Most sold products - Overall (keep existing)
    most_sold_products = Product.objects.annotate(
        sales_count=Count('transactions', filter=Q(transactions__transaction_status='approved')),
        total_revenue=Sum('transactions__amount', filter=Q(transactions__transaction_status='approved'))
    ).order_by('-sales_count')[:5]

    # Most sold products - Today
    most_sold_today = Product.objects.annotate(
        sales_count=Count('transactions',
            filter=Q(
                transactions__transaction_status='approved',
                transactions__transaction_date__date=today.date()
            )),
        total_revenue=Sum('transactions__amount',
            filter=Q(
                transactions__transaction_status='approved',
                transactions__transaction_date__date=today.date()
            ))
    ).order_by('-sales_count')[:5]

    # Most sold products - This Week
    most_sold_week = Product.objects.annotate(
        sales_count=Count('transactions',
            filter=Q(
                transactions__transaction_status='approved',
                transactions__transaction_date__gte=week_ago
            )),
        total_revenue=Sum('transactions__amount',
            filter=Q(
                transactions__transaction_status='approved',
                transactions__transaction_date__gte=week_ago
            ))
    ).order_by('-sales_count')[:5]

    # Get all customers with their total spent
    all_customers = CustomUser.objects.annotate(
        total_spent=Sum('transactions__amount',
                       filter=Q(transactions__transaction_status='approved'))
    ).exclude(total_spent=None).order_by('-total_spent')

    # Get top 5 customers for initial display
    top_customers = all_customers[:5]


    # Weekly revenue data for chart
    weekly_revenue = Transaction.objects.filter(
        transaction_status='approved'
    ).annotate(
        week=TruncWeek('transaction_date')
    ).values('week').annotate(
        total=Sum('amount')
    ).order_by('week')

    # Category distribution
    category_distribution = Category.objects.annotate(
        product_count=Count('products')
    ).values('name', 'product_count')

    # Create chart data
    line_chart = RevenueChartView()
    revenue_chart_data = {
        'labels': line_chart.get_labels(),
        'datasets': [{
            'label': 'Weekly Revenue',
            'data': line_chart.get_data()[0],
            'backgroundColor': 'rgba(52, 152, 219, 0.1)',
            'borderColor': '#3498db',
            'borderWidth': 2,
        }]
    }

    context = {
        'total_revenue': total_revenue,
        'recent_revenue': recent_revenue,
        'most_sold_products': most_sold_products,
        'most_sold_today': most_sold_today,
        'most_sold_week': most_sold_week,
        'top_customers': top_customers,
        'all_customers': all_customers,
        'weekly_revenue': list(weekly_revenue),
        'category_distribution': list(category_distribution),
        't_products': t_products,
        't_users': t_users,
        'revenue_chart_data': revenue_chart_data,
    }

    return render(request, 'BestLogMarketPlaceApp/custom_admin.html', context)

@staff_member_required
def custom_admin_products(request):
    # Get all products with their related category and transaction count
    products = Product.objects.annotate(
        transaction_count=Count('transactions', filter=Q(transactions__transaction_status='approved')),
        total_revenue=Sum('transactions__amount', filter=Q(transactions__transaction_status='approved'))
    ).select_related('category').order_by('-transaction_count')

    # Get category filters
    categories = Category.objects.all()

    context = {
        'products': products,
        'categories': categories,
    }

    return render(request, 'BestLogMarketPlaceApp/custom_admin_products.html', context)

@staff_member_required
def custom_admin_users(request):
    users = CustomUser.objects.annotate(
        total_spent=Sum('transactions__amount',
                       filter=Q(transactions__transaction_status='approved')),
        transaction_count=Count('transactions',
                              filter=Q(transactions__transaction_status='approved')),
        last_transaction=Max('transactions__transaction_date')
    ).order_by('-date_joined')

    context = {
        'users': users,
    }
    return render(request, 'BestLogMarketPlaceApp/custom_admin_users.html', context)

from django.db.models import Sum, Count, F, ExpressionWrapper, DurationField
from django.utils.timezone import now
from django.db.models import Q
# views.py
@staff_member_required
def custom_admin_transactions(request):
    transactions = Transaction.objects.select_related('user').annotate(
        days_since_created=ExpressionWrapper(
            now() - F('transaction_date'),
            output_field=DurationField()
        )
    ).order_by('-transaction_date')

    # Calculate total amounts
    total_approved = transactions.filter(transaction_status='approved').aggregate(
        Sum('amount'))['amount__sum'] or 0
    total_pending = transactions.filter(transaction_status='pending').aggregate(
        Sum('amount'))['amount__sum'] or 0

    context = {
        'transactions': transactions,
        'total_approved': total_approved,
        'total_pending': total_pending,
        'total_transactions': transactions.count(),
    }
    return render(request, 'BestLogMarketPlaceApp/custom_admin_transactions.html', context)
#   search_query = request.GET.get('search', '')  # Get the search query from URL parameters

#   if search_query:
#     # Filter categories by name (case-insensitive)
#     categories = Category.objects.filter(name__icontains=search_query)
#     products = Product.objects.filter(category__in=categories)  # Find products in matching categories
#   else:
#     categories = Category.objects.all()
#     products = Product.objects.all()  # Display all categories and products if no query

#   context = {'categories': categories, 'products': products, 'search_query': search_query}
#   return render(request, 'BestLogMarketPlaceApp/index.html', context)