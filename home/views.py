import logging
from decimal import Decimal, InvalidOperation
import uuid
from django.conf import settings

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.contrib.auth import login as auth_login, logout as auth_logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

logger = logging.getLogger(__name__)

from .forms import CustomerRegistrationForm
from .models_product_media import ProductMedia
from .indexnow import submit_urls
from .notification_service import notify_user, notify_wishlist_product_change
from .forms import CustomerRegistrationForm, SellerRegistrationForm, SellerProductForm, ProductReviewForm, catalog_browser_choices, resolve_catalog_item
from .models import CustomerAddress, DeliveryAgent, DeliveryLocationPing, Order, OrderEvent, OrderItem, Product, ProductReview, SellerPayoutRequest, SellerProfile, SellerSettlement, SellerWallet, WishlistItem


def _is_seller_user(user):
    return bool(getattr(user, "seller_profile", None))


def _is_delivery_user(user):
    return bool(getattr(user, "delivery_agent_profile", None))


def _customer_only(request):
    """True only for a genuine customer session; seller/admin/delivery are excluded."""
    user = request.user
    return bool(
        user.is_authenticated
        and not user.is_staff
        and not user.is_superuser
        and not _is_seller_user(user)
        and not _is_delivery_user(user)
    )

def _customer_boundary_redirect(request):
    """Route an authenticated non-customer back to the correct portal."""
    user = request.user
    if user.is_staff or user.is_superuser:
        return redirect("/admin/")
    if _is_seller_user(user):
        return redirect("seller_dashboard")
    if _is_delivery_user(user):
        return redirect("delivery_portal")
    return redirect("customer_login")


def _record_order_event(order, event_type, note="", actor=None, delivery_agent=None):
    return OrderEvent.objects.create(order=order, event_type=event_type, note=note, actor=actor, delivery_agent=delivery_agent)



def _notify_product_indexnow(product):
    site = str(getattr(settings, "PUBLIC_SITE_URL", "https://shopivakenya.top") or "https://shopivakenya.top").rstrip("/")
    submit_urls([f"{site}/product/{product.id}/"])

def customer_register(request):
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            return redirect("/admin/")
        if _is_seller_user(request.user):
            messages.info(request, "You are signed in to a seller account. Customer accounts are separate.")
            return redirect("seller_dashboard")
        if _is_delivery_user(request.user):
            messages.info(request, "Delivery accounts use a separate portal.")
            return redirect("delivery_portal")
        return redirect("customer_dashboard")
    if request.method == "POST":
        form = CustomerRegistrationForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    user = form.save()
            except IntegrityError:
                form.add_error("username", "Username exists. Please choose a different username.")
            else:
                auth_login(request, user)
                messages.success(request, f"Welcome to Shopiva, {user.username}!")
                return redirect("customer_dashboard")
    else:
        form = CustomerRegistrationForm()
    return render(request, "accounts/register.html", {"form": form})


def customer_login(request):
    """Authenticate only customer accounts; seller/delivery/admin identities are rejected."""
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            messages.info(request, "Admin accounts can only be used in the Shopiva Admin Control Center.")
            return redirect("/admin/")
        if _is_seller_user(request.user):
            messages.info(request, "You are signed in as a seller. Use the Seller Workspace.")
            return redirect("seller_dashboard")
        if _is_delivery_user(request.user):
            messages.info(request, "Delivery accounts use the Delivery Portal.")
            return redirect("delivery_portal")
        return redirect("customer_dashboard")
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if user.is_staff or user.is_superuser:
                form.add_error(None, "This is an admin account. Please use the Shopiva Admin Control Center.")
            elif _is_seller_user(user):
                form.add_error(None, "This is a seller account. Please use the dedicated Seller Login.")
            elif _is_delivery_user(user):
                form.add_error(None, "This is a delivery account. Please use the Delivery Portal.")
            else:
                auth_login(request, user)
                messages.success(request, f"Welcome back, {user.username}!")
                return redirect("customer_dashboard")
    else:
        form = AuthenticationForm()
    return render(request, "accounts/login.html", {"form": form})


def customer_logout(request):
    was_admin = bool(
        request.user.is_authenticated
        and (request.user.is_staff or request.user.is_superuser)
    )
    auth_logout(request)
    if was_admin:
        return redirect("admin_login")
    messages.success(request, "You have been signed out of Shopiva.")
    return redirect("customer_login")


@login_required(login_url="customer_login")
def customer_dashboard(request):
    if not _customer_only(request):
        return _customer_boundary_redirect(request)

    # The account landing page must remain usable even if an older/partially
    # migrated order record cannot be loaded. A broken order relation should
    # never prevent a customer from reaching the account controls or signing out.
    orders = Order.objects.none()
    latest_order = None
    try:
        orders = Order.objects.filter(customer=request.user).order_by("-created_at")
        latest_order = orders.first()
    except Exception:
        logger.exception("Customer dashboard could not load orders for user %s", request.user.pk)

    return render(
        request,
        "accounts/dashboard.html",
        {"orders": orders[:5], "latest_order": latest_order},
    )


@login_required(login_url="customer_login")
def customer_orders(request):
    if not _customer_only(request):
        return redirect("/admin/")
    orders = Order.objects.filter(customer=request.user).prefetch_related("events", "delivery_agent").order_by("-created_at")
    return render(request, "accounts/orders.html", {"orders": orders})


@login_required(login_url="customer_login")
def reorder_order(request, order_id):
    if not _customer_only(request):
        return _customer_boundary_redirect(request)
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    order = get_object_or_404(
        Order.objects.prefetch_related("items__product"),
        id=order_id,
        customer=request.user,
        status="delivered",
    )
    cart_data = request.session.get("cart", {})
    added = 0
    skipped = []

    for item in order.items.select_related("product"):
        product = item.product
        if not product.is_active or product.stock_quantity <= 0:
            skipped.append(product.name)
            continue
        try:
            current = max(0, int(cart_data.get(str(product.id), 0)))
        except (TypeError, ValueError):
            current = 0
        quantity = min(item.quantity, max(0, product.stock_quantity - current))
        if quantity <= 0:
            skipped.append(product.name)
            continue
        cart_data[str(product.id)] = current + quantity
        added += quantity

    request.session["cart"] = cart_data
    request.session.modified = True
    if added:
        messages.success(request, f"{added} item(s) from Order #{order.id} were added to your cart for a quick repeat purchase.")
    if skipped:
        messages.warning(request, "Some items were skipped because they are currently unavailable or out of stock.")
    return redirect("cart")


@login_required(login_url="customer_login")
def customer_delivery_location(request):
    if not _customer_only(request):
        return JsonResponse({"ok": False, "error": "Admin accounts use the admin delivery map."}, status=403)
    latest_order = (Order.objects.filter(customer=request.user).select_related("delivery_agent").prefetch_related("events__delivery_agent").order_by("-created_at").first())
    if not latest_order or not latest_order.delivery_agent:
        return JsonResponse({"ok": True, "agent": None, "order": None, "events": []})
    agent = latest_order.delivery_agent
    latest_ping = agent.location_history.order_by("-recorded_at").first()
    data = {"id": agent.id, "name": agent.display_name, "phone": agent.phone or "", "vehicle_type": agent.vehicle_type or "", "vehicle_number": agent.vehicle_number or "", "status": agent.get_status_display(), "latitude": float(agent.current_latitude) if agent.current_latitude is not None else None, "longitude": float(agent.current_longitude) if agent.current_longitude is not None else None, "updated": agent.last_location_at.isoformat() if agent.last_location_at else None, "live": agent.location_is_live, "accuracy": float(latest_ping.accuracy_meters) if latest_ping and latest_ping.accuracy_meters is not None else None, "speed_mps": float(latest_ping.speed_mps) if latest_ping and latest_ping.speed_mps is not None else None, "heading_degrees": float(latest_ping.heading_degrees) if latest_ping and latest_ping.heading_degrees is not None else None}
    events = [{"type": event.event_type, "label": event.get_event_type_display(), "note": event.note or "", "created": event.created_at.isoformat()} for event in latest_order.events.all()[:8]]
    return JsonResponse({"ok": True, "agent": data, "order": {"id": latest_order.id, "tracking_code": latest_order.tracking_code, "status": latest_order.get_status_display(), "created": latest_order.created_at.isoformat(), "address": latest_order.address}, "events": events})


@login_required(login_url="customer_login")
def customer_profile(request):
    if not _customer_only(request):
        return redirect("/admin/")
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        if email and email != request.user.email.lower():
            messages.error(request, "Email changes are locked for account security. Contact Shopiva support for a verified email change.")
            return redirect("customer_profile")
        messages.success(request, "Your profile is up to date.")
        return redirect("customer_profile")
    return render(request, "accounts/profile.html")


@login_required(login_url="customer_login")
def customer_addresses(request):
    if not _customer_only(request):
        return redirect("/admin/")
    if request.method == "POST":
        action = request.POST.get("action", "save")
        address_id = request.POST.get("address_id")
        if action == "delete" and address_id:
            CustomerAddress.objects.filter(id=address_id, user=request.user).delete()
            messages.success(request, "Address removed.")
            return redirect("customer_addresses")
        if action == "default" and address_id:
            with transaction.atomic():
                CustomerAddress.objects.filter(user=request.user).update(is_default=False)
                CustomerAddress.objects.filter(id=address_id, user=request.user).update(is_default=True)
            messages.success(request, "Default delivery address updated.")
            return redirect("customer_addresses")
        fields = {"label": request.POST.get("label", "Home").strip() or "Home", "full_name": request.POST.get("full_name", "").strip(), "phone": request.POST.get("phone", "").strip(), "county": request.POST.get("county", "").strip(), "town": request.POST.get("town", "").strip(), "address_line": request.POST.get("address_line", "").strip(), "landmark": request.POST.get("landmark", "").strip()}
        if not all(fields[key] for key in ("full_name", "phone", "county", "town", "address_line")):
            messages.error(request, "Please complete your name, phone, county, town and address.")
        else:
            with transaction.atomic():
                if not CustomerAddress.objects.filter(user=request.user).exists():
                    fields["is_default"] = True
                address = CustomerAddress.objects.create(user=request.user, **fields)
                if address.is_default:
                    CustomerAddress.objects.filter(user=request.user).exclude(id=address.id).update(is_default=False)
            messages.success(request, "Delivery address saved.")
            return redirect("customer_addresses")
    addresses = CustomerAddress.objects.filter(user=request.user)
    return render(request, "accounts/addresses.html", {"addresses": addresses})


@login_required(login_url="customer_login")
def customer_wishlist(request):
    if not _customer_only(request):
        return redirect("/admin/")
    if request.method == "POST":
        product_id = request.POST.get("product_id")
        action = request.POST.get("action", "toggle")
        product = get_object_or_404(Product, id=product_id, is_active=True)
        item = WishlistItem.objects.filter(user=request.user, product=product).first()
        if action == "remove":
            if item:
                item.delete()
                messages.success(request, f"{product.name} removed from your wishlist.")
        else:
            if item:
                item.delete()
                messages.info(request, f"{product.name} removed from your wishlist.")
            else:
                WishlistItem.objects.create(user=request.user, product=product)
                messages.success(request, f"{product.name} saved to your wishlist.")
        return redirect("customer_wishlist")
    wishlist = WishlistItem.objects.filter(user=request.user).select_related("product")
    return render(request, "accounts/wishlist.html", {"wishlist": wishlist})


@login_required(login_url="delivery_login")
def delivery_portal(request):
    try:
        agent = request.user.delivery_agent_profile
    except DeliveryAgent.DoesNotExist:
        return redirect("/admin/")
    if not agent.is_active:
        return render(request, "delivery/not_authorized.html")
    assigned_orders = agent.orders.select_related("delivery_agent").prefetch_related("events").exclude(status="delivered").exclude(status="cancelled").order_by("-created_at")
    return render(request, "delivery/portal.html", {"agent": agent, "assigned_orders": assigned_orders})


@login_required(login_url="delivery_login")
def delivery_update_location(request):
    if request.method != "POST":
        return redirect("delivery_portal")
    try:
        agent = request.user.delivery_agent_profile
    except DeliveryAgent.DoesNotExist:
        return redirect("/admin/")
    if not agent.is_active:
        return redirect("/admin/")
    try:
        latitude = Decimal(request.POST.get("latitude", ""))
        longitude = Decimal(request.POST.get("longitude", ""))
    except (InvalidOperation, TypeError):
        return redirect("delivery_portal")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return redirect("delivery_portal")
    now = timezone.now()
    agent.current_latitude = latitude.quantize(Decimal("0.000001"))
    agent.current_longitude = longitude.quantize(Decimal("0.000001"))
    agent.last_location_at = now
    agent.status = "on_delivery" if agent.orders.filter(status="out_for_delivery").exists() else "available"
    agent.save(update_fields=["current_latitude", "current_longitude", "last_location_at", "status"])
    DeliveryLocationPing.objects.create(agent=agent, latitude=agent.current_latitude, longitude=agent.current_longitude)
    return redirect("delivery_portal")


@login_required(login_url="delivery_login")
def delivery_ping_location(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    try:
        agent = request.user.delivery_agent_profile
    except DeliveryAgent.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Delivery partner profile not found."}, status=403)
    if not agent.is_active:
        return JsonResponse({"ok": False, "error": "Delivery partner account is inactive."}, status=403)
    try:
        latitude = Decimal(str(request.POST.get("latitude", "")))
        longitude = Decimal(str(request.POST.get("longitude", "")))
    except (InvalidOperation, TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid coordinates."}, status=400)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return JsonResponse({"ok": False, "error": "Coordinates are out of range."}, status=400)

    def optional_decimal(field_name, minimum=None, maximum=None):
        raw = request.POST.get(field_name, "").strip()
        if not raw:
            return None
        try:
            value = Decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if minimum is not None and value < minimum:
            return None
        if maximum is not None and value > maximum:
            return None
        return value

    accuracy = optional_decimal("accuracy", minimum=Decimal("0"), maximum=Decimal("100000"))
    speed = optional_decimal("speed", minimum=Decimal("0"), maximum=Decimal("100"))
    heading = optional_decimal("heading", minimum=Decimal("0"), maximum=Decimal("360"))
    now = timezone.now()
    latitude = latitude.quantize(Decimal("0.000001"))
    longitude = longitude.quantize(Decimal("0.000001"))
    agent.current_latitude = latitude
    agent.current_longitude = longitude
    agent.last_location_at = now
    agent.status = "on_delivery" if agent.orders.filter(status="out_for_delivery").exists() else "available"
    agent.save(update_fields=["current_latitude", "current_longitude", "last_location_at", "status"])
    DeliveryLocationPing.objects.create(agent=agent, latitude=latitude, longitude=longitude, accuracy_meters=accuracy.quantize(Decimal("0.01")) if accuracy is not None else None, speed_mps=speed.quantize(Decimal("0.01")) if speed is not None else None, heading_degrees=heading.quantize(Decimal("0.01")) if heading is not None else None)
    return JsonResponse({"ok": True, "updated_at": now.isoformat(), "latitude": float(latitude), "longitude": float(longitude), "status": agent.get_status_display()})


def home(request):
    """High-conversion Kenyan storefront with bounded queries and dynamic marketplace signals."""
    try:
        base_products = Product.objects.filter(is_active=True).select_related("seller").order_by("-id")
        homepage_products = list(base_products[:24])
        discounted_products = list(base_products.filter(discount_percent__gt=0).order_by("-discount_percent", "-id")[:8])
        featured_products = list(base_products.filter(is_featured=True)[:8])
        seen_ids = {product.id for product in featured_products}
        for product in discounted_products + homepage_products:
            if product.id not in seen_ids and len(featured_products) < 8:
                featured_products.append(product)
                seen_ids.add(product.id)
        budget_products = {
            "under_1000": list(base_products.filter(price__lte=1000)[:4]),
            "under_5000": list(base_products.filter(price__lte=5000, price__gt=1000)[:4]),
            "under_10000": list(base_products.filter(price__lte=10000, price__gt=5000)[:4]),
        }
        return render(request, "home.html", {"products": homepage_products, "featured_products": featured_products, "discounted_products": discounted_products, "budget_products": budget_products, "active_product_count": base_products.count(), "seller_count": SellerProfile.objects.filter(is_active=True).count(), "discounted_product_count": base_products.filter(discount_percent__gt=0).count(), "search_suggestions": [p.name for p in homepage_products[:12]]})
    except Exception:
        logger.exception("SHOPIVA_HOME_REQUEST_FAILED path=%s", request.path)
        raise


def categories(request):
    db_categories = list(
        Product.objects.filter(is_active=True)
        .exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )
    catalog_categories = []
    seen = set()
    for row in catalog_browser_choices():
        category = str(row["category"]).strip()
        key = category.casefold()
        if category and key not in seen:
            seen.add(key)
            catalog_categories.append(category)
        if len(catalog_categories) >= 40:
            break
    categories = db_categories + [
        category for category in catalog_categories
        if category.casefold() not in {item.casefold() for item in db_categories}
    ]
    return render(request, "categories.html", {"categories": categories[:40], "catalog_categories": catalog_categories})


def product_detail(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    related_products = list(
        Product.objects.filter(is_active=True, category__iexact=product.category)
        .exclude(id=product.id)
        .select_related("seller")
        .order_by("-is_featured", "-id")[:6]
    )
    if len(related_products) < 6:
        extra = Product.objects.filter(is_active=True).exclude(
            id__in=[product.id, *(p.id for p in related_products)]
        ).select_related("seller").order_by("-is_featured", "-id")[: 6 - len(related_products)]
        related_products.extend(extra)
    return render(
        request,
        "product_detail.html",
        {"product": product, "related_products": related_products},
    )


def add_to_cart(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    cart_data = request.session.get("cart", {})
    product_id_str = str(product.id)
    current_quantity = int(cart_data.get(product_id_str, 0))
    if product.stock_quantity > current_quantity:
        cart_data[product_id_str] = current_quantity + 1
        request.session["cart"] = cart_data
        request.session.modified = True
    return redirect("cart")


def _cart_items(cart_data):
    items = []
    total = Decimal("0.00")
    for product_id, raw_quantity in cart_data.items():
        try:
            product = Product.objects.get(id=product_id, is_active=True)
            quantity = max(0, int(raw_quantity))
        except (Product.DoesNotExist, TypeError, ValueError):
            continue
        if quantity <= 0 or product.stock_quantity <= 0:
            continue
        quantity = min(quantity, product.stock_quantity)
        unit_price = product.discounted_price
        subtotal = unit_price * quantity
        total += subtotal
        items.append({"product": product, "quantity": quantity, "subtotal": subtotal, "unit_price": unit_price})
    return items, total


def cart(request):
    cart_data = request.session.get("cart", {})
    if request.method == "POST":
        action = request.POST.get("action", "")
        product_id = request.POST.get("product_id", "").strip()
        if product_id:
            try:
                product = Product.objects.get(id=product_id, is_active=True)
            except (Product.DoesNotExist, ValueError, TypeError):
                product = None
            if product is not None:
                if action == "remove":
                    cart_data.pop(str(product.id), None)
                elif action == "update":
                    try:
                        quantity = int(request.POST.get("quantity", "1"))
                    except (TypeError, ValueError):
                        quantity = 1
                    quantity = max(0, min(quantity, product.stock_quantity))
                    if quantity == 0:
                        cart_data.pop(str(product.id), None)
                    else:
                        cart_data[str(product.id)] = quantity
                request.session["cart"] = cart_data
                request.session.modified = True
        return redirect("cart")
    items, total = _cart_items(cart_data)
    request.session["cart"] = {str(item["product"].id): item["quantity"] for item in items}
    request.session.modified = True
    return render(request, "cart.html", {"items": items, "total": total})


def checkout(request):
    """Compatibility shim: all customer checkout traffic uses the modern map checkout."""
    return redirect("checkout")

def order_success(request, order_id):
    """Show an order confirmation only to its authenticated owner or bearer-token holder."""
    if request.user.is_authenticated:
        if not _customer_only(request):
            return _customer_boundary_redirect(request)
        order = get_object_or_404(Order, id=order_id, customer=request.user)
    else:
        raw_token = request.GET.get("token", "").strip()
        if not raw_token:
            raise Http404
        order = get_object_or_404(Order, id=order_id, access_token=raw_token)
    latest_payment = order.payments.order_by("-created_at").first()
    payment_method = latest_payment.method if latest_payment else ""
    return render(
        request,
        "order_success.html",
        {"order": order, "payment_method": payment_method},
    )


def products(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    discount_only = request.GET.get("discount", "").strip().lower() in {"1", "true", "yes", "on"}
    sort = request.GET.get("sort", "newest").strip().lower()
    min_price_raw = request.GET.get("min_price", "").strip()
    max_price_raw = request.GET.get("max_price", "").strip()
    product_list = Product.objects.filter(is_active=True)
    if query:
        product_list = product_list.filter(Q(name__icontains=query) | Q(description__icontains=query) | Q(category__icontains=query))
    if category:
        product_list = product_list.filter(category__iexact=category)
    if discount_only:
        product_list = product_list.filter(discount_percent__gt=0)
    try:
        if min_price_raw:
            product_list = product_list.filter(price__gte=Decimal(min_price_raw))
    except (InvalidOperation, TypeError):
        min_price_raw = ""
    try:
        if max_price_raw:
            product_list = product_list.filter(price__lte=Decimal(max_price_raw))
    except (InvalidOperation, TypeError):
        max_price_raw = ""
    order_map = {"newest": "-id", "price_low": "price", "price_high": "-price", "discount": "-discount_percent", "name": "name"}
    product_list = product_list.order_by(order_map.get(sort, "-id"))
    db_categories = list(
        Product.objects.filter(is_active=True)
        .exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )
    catalog_categories = []
    seen_categories = set()
    for row in catalog_browser_choices():
        catalog_category = str(row["category"]).strip()
        key = catalog_category.casefold()
        if catalog_category and key not in seen_categories:
            seen_categories.add(key)
            catalog_categories.append(catalog_category)
        if len(catalog_categories) >= 40:
            break
    categories_list = db_categories + [
        category for category in catalog_categories
        if category.casefold() not in {item.casefold() for item in db_categories}
    ]
    paginator = Paginator(product_list, 12)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "products.html", {"products": page_obj, "page_obj": page_obj, "categories": categories_list[:40], "catalog_categories": catalog_categories, "query": query, "selected_category": category, "discount_only": discount_only, "selected_sort": sort, "min_price": min_price_raw, "max_price": max_price_raw})


@login_required(login_url="customer_login")
def product_review(request, product_id):
    if not _customer_only(request):
        return redirect("/admin/")
    product = get_object_or_404(Product, id=product_id)
    eligible_orders = Order.objects.filter(customer=request.user, status="delivered", items__product=product).distinct()
    if not eligible_orders.exists():
        messages.error(request, "You can review this product only after a delivered purchase.")
        return redirect("product_detail", product_id=product.id)
    if request.method == "POST":
        order = get_object_or_404(eligible_orders, id=request.POST.get("order_id"))
        form = ProductReviewForm(request.POST)
        if form.is_valid():
            review = form.save(commit=False)
            review.product = product
            review.customer = request.user
            review.order = order
            review.save()
            messages.success(request, "Thank you. Your verified review is now live.")
            return redirect("product_detail", product_id=product.id)
    else:
        form = ProductReviewForm()
    return render(request, "reviews/form.html", {"product": product, "orders": eligible_orders, "form": form})


@login_required(login_url="customer_login")
def customer_notifications(request):
    if not _customer_only(request):
        return redirect("/admin/")
    notifications = request.user.shopiva_notifications.all()[:50]
    request.user.shopiva_notifications.filter(is_read=False).update(is_read=True)
    return render(request, "accounts/notifications.html", {"notifications": notifications})


def _is_customer_only_user(user):
    return bool(
        user.is_authenticated
        and not user.is_staff
        and not user.is_superuser
        and not _is_seller_user(user)
        and not _is_delivery_user(user)
    )


def seller_register(request):
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            return redirect("/admin/")
        if _is_seller_user(request.user):
            return redirect("seller_dashboard")
        if _is_customer_only_user(request.user):
            messages.info(request, "Seller accounts are separate from customer accounts. Sign out before creating a seller account.")
            return redirect("customer_dashboard")
        if _is_delivery_user(request.user):
            messages.info(request, "Delivery accounts cannot be converted into seller accounts.")
            return redirect("delivery_portal")
    if request.method == "POST":
        form = SellerRegistrationForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                seller = SellerProfile.objects.create(
                    user=user,
                    business_name=form.cleaned_data["business_name"].strip(),
                    business_nature=form.cleaned_data["business_nature"].strip(),
                    mpesa_phone=form.cleaned_data["mpesa_phone"].strip(),
                )
                SellerWallet.objects.create(seller=seller)

            # Alert every active administrator immediately when a new seller joins.
            admin_user_model = get_user_model()
            admin_users = admin_user_model.objects.filter(is_staff=True, is_active=True).only("id")
            for admin_user in admin_users:
                notify_user(
                    admin_user,
                    "system",
                    "New seller registration",
                    (
                        f"New seller account: {seller.business_name or seller.user.username}. "
                        f"Nature of business: {seller.business_nature}. "
                        f"Username: {seller.user.username}. Email: {seller.user.email}."
                    ),
                    link="/admin/operations-center/",
                    email=admin_user.email,
                )

            auth_login(request, user)
            messages.success(request, "Seller account created. Add your first product from the seller dashboard.")
            return redirect("seller_dashboard")
    else:
        form = SellerRegistrationForm()
    return render(request, "seller/register.html", {"form": form})


@login_required(login_url="customer_login")
def seller_dashboard(request):
    if request.user.is_staff or request.user.is_superuser:
        return redirect("/admin/")
    seller = getattr(request.user, "seller_profile", None)
    if not seller or not seller.is_active:
        messages.error(request, "Seller workspace access requires an active seller account.")
        return redirect("seller_login")
    wallet, _ = SellerWallet.objects.get_or_create(seller=seller)
    products = seller.products.order_by("-id")
    order_items = seller.order_items.select_related("order", "product").order_by("-id")[:50]
    settlements = seller.settlements.select_related("order").order_by("-created_at")[:25]
    payouts = seller.payout_requests.order_by("-created_at")[:25]
    analytics = {"orders": seller.order_items.values("order_id").distinct().count(), "units": seller.order_items.aggregate(total=Sum("quantity"))["total"] or 0, "gross": seller.order_items.aggregate(total=Sum("seller_gross"))["total"] or Decimal("0.00"), "net": seller.order_items.aggregate(total=Sum("seller_net"))["total"] or Decimal("0.00"), "delivered": seller.order_items.filter(order__status="delivered").values("order_id").distinct().count()}
    inventory = {
        "total": products.count(),
        "live": products.filter(is_active=True).count(),
        "paused": products.filter(is_active=False).count(),
        "out": products.filter(stock_quantity=0).count(),
        "low": products.filter(stock_quantity__gt=0, stock_quantity__lte=5).count(),
        "units": products.aggregate(total=Sum("stock_quantity"))["total"] or 0,
    }
    return render(request, "seller/dashboard.html", {"seller": seller, "wallet": wallet, "products": products, "order_items": order_items, "settlements": settlements, "payouts": payouts, "analytics": analytics, "notifications": seller.user.shopiva_notifications.all()[:10], "master_catalog": catalog_browser_choices(), "inventory": inventory})


@login_required(login_url="customer_login")
def seller_product_add(request):
    if request.user.is_staff or request.user.is_superuser:
        return redirect("/admin/")
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        return redirect("seller_register")
    if request.method == "POST":
        form = SellerProductForm(request.POST, request.FILES, seller=seller)
        if form.is_valid():
            product = form.save(commit=False)
            product.seller = seller
            product.save()
            for position, image_file in enumerate(getattr(product, "_shopiva_gallery_files", [])[:8]):
                ProductMedia.objects.create(product=product, image=enhance_product_image(image_file, f"{product.name}-gallery-{position + 1}"), position=position)
            messages.success(request, f"{product.name} is now listed on Shopiva.")
            return redirect("seller_dashboard")
    else:
        catalog_value = request.GET.get("catalog", "").strip()
        catalog_item = resolve_catalog_item(catalog_value)
        initial = {}
        if catalog_item:
            initial = {
                "catalog_product": catalog_value,
                "name": catalog_item["name"],
                "category": catalog_item["category"],
            }
        form = SellerProductForm(initial=initial, seller=seller)
    return render(request, "seller/product_form.html", {"form": form, "seller": seller, "mode": "add"})


@login_required(login_url="customer_login")
def seller_product_edit(request, product_id):
    if request.user.is_staff or request.user.is_superuser:
        return redirect("/admin/")
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, id=product_id, seller=seller)
    if request.method == "POST":
        old_price, old_discount, old_stock = product.price, product.discount_percent, product.stock_quantity
        form = SellerProductForm(request.POST, request.FILES, instance=product, seller=seller)
        if form.is_valid():
            product = form.save()
            notify_wishlist_product_change(product, old_price, old_discount, old_stock, actor_label="Seller update")
            gallery_files = getattr(product, "_shopiva_gallery_files", [])
            if gallery_files:
                product.media.all().delete()
                for position, image_file in enumerate(gallery_files[:8]):
                    ProductMedia.objects.create(product=product, image=enhance_product_image(image_file, f"{product.name}-gallery-{position + 1}"), position=position)
            messages.success(request, f"{product.name} updated.")
            _notify_product_indexnow(product)
            return redirect("seller_dashboard")
    else:
        form = SellerProductForm(instance=product, seller=seller)
    return render(request, "seller/product_form.html", {"form": form, "seller": seller, "product": product, "mode": "edit"})


@login_required(login_url="customer_login")
def seller_order_update(request, order_id):
    if request.method != "POST":
        return redirect("seller_dashboard")
    seller = getattr(request.user, "seller_profile", None)
    if not seller or not seller.is_active:
        return redirect("seller_login")
    allowed = {"confirmed", "packed", "processing", "shipped", "out_for_delivery"}
    next_status = request.POST.get("status", "").strip()
    if next_status not in allowed:
        messages.error(request, "That order action is not available.")
        return redirect("seller_dashboard")
    with transaction.atomic():
        order = get_object_or_404(
            Order.objects.select_for_update(),
            id=order_id,
            items__seller=seller,
        )
        current_items = order.items.filter(seller=seller)
        if not current_items.exists():
            messages.error(request, "This order does not belong to your shop.")
            return redirect("seller_dashboard")
        if order.status in {"delivered", "cancelled"}:
            messages.error(request, "Delivered or cancelled orders cannot be moved back into processing.")
            return redirect("seller_dashboard")
        if next_status == "shipped" and order.status not in {"packed", "processing"}:
            messages.error(request, "Pack the order before marking it shipped.")
            return redirect("seller_dashboard")
        if next_status == "out_for_delivery" and order.status != "shipped":
            messages.error(request, "Mark the order shipped before sending it out for delivery.")
            return redirect("seller_dashboard")
        if next_status == "out_for_delivery" and not order.delivery_agent_id:
            messages.error(request, "Assign a Shopiva delivery partner before sending this order out for delivery.")
            return redirect("seller_dashboard")
        order.status = next_status
        update_fields = ["status"]
        if next_status == "out_for_delivery":
            order.ensure_delivery_confirmation_code()
            update_fields.extend(["delivery_confirmation_code", "delivery_verification_attempts", "delivery_verification_locked_at"])
        if next_status == "packed":
            order.packed_at = timezone.now()
            update_fields.append("packed_at")
        order.save(update_fields=update_fields)
        event_map = {
            "confirmed": ("confirmed", "Seller confirmed the order."),
            "packed": ("packed", "Seller packed the order."),
            "processing": ("processing", "Seller is preparing the order."),
            "shipped": ("shipped", "Order handed over for shipping/delivery."),
            "out_for_delivery": ("out_for_delivery", "Order is out for delivery."),
        }
        event_type, note = event_map[next_status]
        OrderEvent.objects.create(order=order, event_type=event_type, note=note, actor=request.user)
    messages.success(request, f"Order #{order.id} moved to {order.get_status_display()}.")
    return redirect("seller_dashboard")


@login_required(login_url="customer_login")
def seller_product_stock_update(request, product_id):
    if request.method != "POST":
        return redirect("seller_dashboard")
    seller = getattr(request.user, "seller_profile", None)
    if not seller or not seller.is_active:
        return redirect("seller_login")
    try:
        quantity = int(request.POST.get("stock_quantity", "0"))
    except (TypeError, ValueError):
        messages.error(request, "Stock must be a whole number.")
        return redirect("seller_dashboard")
    if quantity < 0 or quantity > 100000000:
        messages.error(request, "Enter a stock quantity between 0 and 100,000,000.")
        return redirect("seller_dashboard")
    with transaction.atomic():
        product = get_object_or_404(
            Product.objects.select_for_update(),
            id=product_id,
            seller=seller,
        )
        product.stock_quantity = quantity
        product.save(update_fields=["stock_quantity"])
    _notify_product_indexnow(product)
    if quantity == 0:
        messages.warning(request, f"{product.name} is now out of stock.")
    elif quantity <= 5:
        messages.warning(request, f"{product.name} stock updated to {quantity}. Low-stock alert.")
    else:
        messages.success(request, f"{product.name} stock updated to {quantity}.")
    return redirect("seller_dashboard")


@login_required(login_url="customer_login")
def seller_product_toggle(request, product_id):
    if request.method != "POST":
        return redirect("seller_dashboard")
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, id=product_id, seller=seller)
    product.is_active = not product.is_active
    product.save(update_fields=["is_active"])
    _notify_product_indexnow(product)
    messages.success(request, f"{product.name} is now {'live' if product.is_active else 'hidden'}.")
    return redirect("seller_dashboard")


@login_required(login_url="customer_login")
def seller_product_delete(request, product_id):
    if request.method != "POST":
        return redirect("seller_dashboard")
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, id=product_id, seller=seller)
    product.is_active = False
    product.save(update_fields=["is_active"])
    _notify_product_indexnow(product)
    messages.success(request, f"{product.name} has been hidden from the marketplace.")
    return redirect("seller_dashboard")


@login_required(login_url="customer_login")
def seller_request_payout(request):
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        return redirect("seller_register")
    wallet = get_object_or_404(SellerWallet, seller=seller)
    if request.method == "POST":
        try:
            amount = Decimal(request.POST.get("amount", "0"))
        except InvalidOperation:
            amount = Decimal("0")
        phone = request.POST.get("phone", "").strip()
        if amount <= 0 or amount > wallet.available_balance or not phone:
            messages.error(request, "Enter a valid payout amount, phone number and keep the request within your available balance.")
        else:
            SellerPayoutRequest.objects.create(seller=seller, amount=amount, phone=phone, idempotency_key=uuid.uuid4().hex)
            messages.success(request, "Payout request submitted for processing.")
    return redirect("seller_dashboard")
