from decimal import Decimal, InvalidOperation
import logging
import re

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .models import Order
from .payments import checkout_mpesa as original_checkout_mpesa
from .delivery_pricing import calculate_order_quote, tariff_text
from .models import DeliveryTariff, DeliveryPickupPoint

logger = logging.getLogger(__name__)


def _coord(value, low, high):
    try:
        value = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return value.quantize(Decimal("0.000001")) if low <= value <= high else None



def pickup_points(request):
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "GET required."}, status=405)
    county = request.GET.get("county", "").strip()
    town = request.GET.get("town", "").strip()
    qs = DeliveryPickupPoint.objects.filter(is_active=True)
    if county:
        qs = qs.filter(county__iexact=county)
    if town:
        qs = qs.filter(town__icontains=town)
    points = list(qs.order_by("town", "name")[:50])
    return JsonResponse({
        "ok": True,
        "pickup_points": [
            {
                "id": point.id,
                "name": point.name,
                "code": point.code,
                "county": point.county,
                "town": point.town,
                "address": point.address,
                "phone": point.phone,
                "partner_name": point.partner_name,
                "max_holding_days": point.max_holding_days,
                "latitude": str(point.latitude),
                "longitude": str(point.longitude),
            }
            for point in points
        ],
    })


def checkout_quote(request):
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "GET required."}, status=405)
    try:
        latitude = _coord(request.GET.get("lat"), Decimal("-90"), Decimal("90")) if request.GET.get("lat") else None
        longitude = _coord(request.GET.get("lng"), Decimal("-180"), Decimal("180")) if request.GET.get("lng") else None
    except Exception:
        latitude = longitude = None

    county = request.GET.get("county", "").strip()
    town = request.GET.get("town", "").strip()
    mode = request.GET.get("delivery_mode", DeliveryTariff.MODE_STANDARD).strip().lower() or DeliveryTariff.MODE_STANDARD
    cart = request.session.get("cart", {})
    items = []
    from .models import Product
    for product_id, raw_quantity in cart.items():
        try:
            product = Product.objects.get(id=product_id, is_active=True)
            quantity = min(max(0, int(raw_quantity)), product.stock_quantity)
        except (Product.DoesNotExist, TypeError, ValueError):
            continue
        if quantity > 0:
            items.append((product, quantity))
    if not items:
        return JsonResponse({"ok": False, "error": "Your cart is empty."}, status=400)

    try:
        quote = calculate_order_quote(items, latitude, longitude, county, town, mode)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    return JsonResponse({
        "ok": True,
        "subtotal": f"{quote['subtotal']:.2f}",
        "commission": f"{quote['commission']:.2f}",
        "delivery_fee": f"{quote['delivery_fee']:.2f}",
        "total": f"{quote['total']:.2f}",
        "distance_km": f"{quote['distance_km']:.2f}" if quote["distance_km"] is not None else None,
        "distance_source": quote["distance_source"],
        "seller_count": quote["seller_count"],
        "county": quote["county"],
        "destination": quote["destination"],
        "delivery_mode": quote["delivery_mode"],
        "tariff_fee_per_seller": f"{quote['tariff_fee_per_seller']:.2f}" if quote["tariff_fee_per_seller"] is not None else None,
        "tariff_scope": quote["tariff_scope"],
        "pricing_basis": quote["pricing_basis"],
        "hub": quote["hub"].name if quote["hub"] else None,
        "base_fee": f"{quote['base_fee']:.2f}",
        "distance_rate": f"{quote['distance_rate']:.2f}",
        "distance_charge": f"{quote['distance_charge']:.2f}",
        "package_class": quote["package_class"],
        "route_class": quote["route_class"],
        "rate_card": quote["rate_card"].name if quote["rate_card"] else None,
        "tariff": tariff_text(),
    })

def checkout_mpesa_map(request, error=None):
    if request.method == "GET":
        cart = request.session.get("cart", {})
        items, total = [], Decimal("0.00")
        from .models import Product
        for product_id, raw_quantity in cart.items():
            try:
                product = Product.objects.get(id=product_id, is_active=True)
                quantity = min(max(0, int(raw_quantity)), product.stock_quantity)
            except (Product.DoesNotExist, TypeError, ValueError):
                continue
            if quantity > 0:
                subtotal = product.discounted_price * quantity
                total += subtotal
                items.append({"product": product, "quantity": quantity, "subtotal": subtotal})
        saved_addresses = []
        user = request.user
        if user.is_authenticated and not user.is_staff and not user.is_superuser:
            from .models import CustomerAddress
            saved_addresses = CustomerAddress.objects.filter(user=user).order_by("-is_default", "-created_at")[:8]
        return render(
            request,
            "customer_checkout_map.html",
            {"items": items, "total": total, "saved_addresses": saved_addresses, "error": error},
        )

    latitude = _coord(request.POST.get("delivery_latitude"), Decimal("-90"), Decimal("90"))
    longitude = _coord(request.POST.get("delivery_longitude"), Decimal("-180"), Decimal("180"))
    if latitude is None or longitude is None:
        messages.error(request, "Select the exact delivery location on the map before continuing to payment.")
        return checkout_mpesa_map(_MapGetRequest(request))

    try:
        response = original_checkout_mpesa(request)
    except Exception:
        logger.exception("Unexpected checkout failure for the map checkout")
        return checkout_mpesa_map(_MapGetRequest(request), error="Checkout could not be completed right now. Your cart and delivery details are still safe; please review them and try again.")

    if getattr(response, "status_code", 200) >= 500:
        logger.error("Checkout returned HTTP %s for the map checkout", response.status_code)
        return checkout_mpesa_map(_MapGetRequest(request), error="Checkout could not be completed right now. No order was confirmed. Please review the delivery and payment details and try again.")

    match = re.search(r"/(?:order-success|payments/mpesa/waiting)/(\d+)/", getattr(response, "url", ""))
    if match:
        order_id = int(match.group(1))
        updates = {"delivery_latitude": latitude, "delivery_longitude": longitude}
        if request.user.is_authenticated and not request.user.is_staff and not request.user.is_superuser:
            Order.objects.filter(pk=order_id, customer=request.user).update(**updates)
        else:
            Order.objects.filter(pk=order_id, email__iexact=request.POST.get("email", "").strip()).update(**updates)
        if "/payments/mpesa/waiting/" in response.url:
            # The map checkout owns browser navigation after the nested payment flow.
            return redirect("mpesa_waiting", order_id=order_id)
    return response


class _MapGetRequest:
    def __init__(self, request):
        self.session = request.session
        self.user = request.user
        self.POST = request.POST
        self.FILES = {}
        self.META = request.META
        self.COOKIES = request.COOKIES
    method = "GET"
