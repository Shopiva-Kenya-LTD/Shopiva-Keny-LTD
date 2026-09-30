import base64
import json
import logging
import os       
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_UP

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .commission import get_platform_commission_percent
from .delivery_pricing import calculate_order_quote
from .models import DeliveryTariff, DeliveryPickupPoint, Order, OrderEvent, OrderItem, PaymentTransaction, Product, SellerSettlement, SellerWallet
from .notifications import notify_user

logger = logging.getLogger(__name__)


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def mpesa_production_ready():
    """Return True only when the required production credentials and Shopiva callback are configured."""
    if _env("MPESA_ENV", "sandbox").lower() != "production":
        return False

    required = (
        "MPESA_CONSUMER_KEY",
        "MPESA_CONSUMER_SECRET",
        "MPESA_PASSKEY",
        "MPESA_CALLBACK_URL",
        "MPESA_SHORTCODE",
    )
    if not all(_env(name) for name in required):
        return False

    # Production must use the Safaricom-approved Till/Store number explicitly.
    # Do not fall back to MPESA_SHORTCODE: mixing a shortcode from another
    # merchant profile with the production credentials can trigger a provider
    # merchant-validation rejection before any customer prompt is sent.
    if not _env("MPESA_TILL_NUMBER"):
        return False

    callback = _env("MPESA_CALLBACK_URL").lower()
    return callback.startswith("https://shopivakenya.top/") or callback.startswith("https://www.shopivakenya.top/")




def _base_url():
    return "https://sandbox.safaricom.co.ke" if _env("MPESA_ENV", "sandbox").lower() == "sandbox" else "https://api.safaricom.co.ke"


def _shortcode():
    if _env("MPESA_ENV", "sandbox").lower() == "production":
        shortcode = _env("MPESA_SHORTCODE")
        if not shortcode:
            raise RuntimeError("M-PESA production Short Code is not configured.")
        return shortcode
    return _env("MPESA_SHORTCODE") or "174379"

def _passkey():
    return _env("MPESA_PASSKEY")

def _till_number():
    till = _env("MPESA_TILL_NUMBER")
    if not till:
        raise RuntimeError("M-PESA Till Number is not configured.")
    return till


def _callback_url():
    return _env("MPESA_CALLBACK_URL", "https://shopivakenya.top/payments/mpesa/callback/")


def normalize_phone(phone):
    value = "".join(ch for ch in str(phone) if ch.isdigit() or ch == "+")
    if value.startswith("+254"):
        return "254" + value[4:]
    if value.startswith("254"):
        return value
    if value.startswith("0") and len(value) == 10:
        return "254" + value[1:]
    if len(value) == 9 and value[0] in {"1", "7"}:
        return "254" + value
    return value


def _request_json(url, data=None, headers=None, method=None):
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8") if isinstance(data, (dict, list)) else data
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers or {},
        method=method or ("POST" if body else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload
    except Exception as exc:
        return 0, {"error": str(exc)}


def daraja_access_token():
    key = _env("MPESA_CONSUMER_KEY")
    secret = _env("MPESA_CONSUMER_SECRET")
    if not key or not secret:
        raise RuntimeError("MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET are not configured.")
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    status, payload = _request_json(
        f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}"},
        method="GET",
    )
    token = payload.get("access_token")
    if status != 200 or not token:
        raise RuntimeError(f"Daraja authentication failed: {payload}")
    return token


def initiate_mpesa_stk(order, payment, phone):
    token = daraja_access_token()
    shortcode = _shortcode()
    passkey = _passkey()
    if not shortcode or not passkey:
        raise RuntimeError("M-PESA production Till/Store number and passkey are not configured.")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
    amount_decimal = Decimal(order.total_amount).quantize(Decimal("0.01"))
    whole_amount = amount_decimal.quantize(Decimal("1"), rounding=ROUND_UP)
    if whole_amount != amount_decimal:
        adjustment = whole_amount - amount_decimal
        order.total_amount = whole_amount
        order.platform_commission_amount = (
            Decimal(order.platform_commission_amount or "0.00") + adjustment
        ).quantize(Decimal("0.01"))
        order.save(update_fields=["total_amount", "platform_commission_amount"])
        amount_decimal = whole_amount
    amount = max(1, int(amount_decimal))
    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        # Shopiva uses a Safaricom Till / Buy Goods merchant.
        "TransactionType": "CustomerBuyGoodsOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": _till_number(),
        "PhoneNumber": phone,
        "CallBackURL": _callback_url(),
        "AccountReference": f"SHOPIVA-{order.id}",
        "TransactionDesc": f"Shopiva order {order.id}",
    }
    status, response = _request_json(
        f"{_base_url()}/mpesa/stkpush/v1/processrequest",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    payment.raw_response = response
    payment.updated_at = timezone.now()
    payment.save(update_fields=["raw_response", "updated_at"])
    if status not in (200, 201) or str(response.get("ResponseCode", "0")) != "0":
        error_code = str(response.get("errorCode") or response.get("ResponseCode") or "").strip()
        error_message = response.get("errorMessage") or response.get("ResponseDescription") or str(response)
        if error_code == "2002" or "Agent number and Store number entered do not match" in str(error_message):
            raise RuntimeError(
                "Safaricom rejected the M-PESA merchant configuration (2002). "
                "The production Till/Store number does not match the approved "
                "Daraja production merchant profile. No customer STK prompt was sent. "
                "Verify the approved Till/Store number, production app credentials, "
                "and passkey with Safaricom before retrying."
            )
        raise RuntimeError(error_message)
    payment.merchant_request_id = response.get("MerchantRequestID", "")
    payment.checkout_request_id = response.get("CheckoutRequestID", "")
    payment.status = "pending"
    payment.save(update_fields=["merchant_request_id", "checkout_request_id", "status", "updated_at"])
    return response



def query_mpesa_stk(payment):
    """Ask Daraja for the current STK request state when a callback has not arrived."""
    checkout_request_id = str(payment.checkout_request_id or "").strip()
    if not checkout_request_id:
        raise RuntimeError("No M-PESA CheckoutRequestID is available for this payment.")

    shortcode = _shortcode()
    passkey = _passkey()
    if not shortcode or not passkey:
        raise RuntimeError("M-PESA shortcode/passkey is not configured.")

    token = daraja_access_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
    status, response = _request_json(
        f"{_base_url()}/mpesa/stkpushquery/v1/query",
        data={
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        },
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if status not in (200, 201):
        raise RuntimeError(response.get("errorMessage") or response.get("ResponseDescription") or str(response))
    return response


def _safe_order_notification(user_id, order_id, tracking):
    """Order notifications are post-commit side effects and must never block checkout."""
    try:
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.get(id=user_id)
        notify_user(
            user,
            "Order placed",
            f"Your Shopiva order {tracking} has been placed and is awaiting payment confirmation.",
            "order",
            f"/account/orders/{order_id}/",
        )
    except Exception:
        logger.exception("Non-blocking order notification failed for order %s", order_id)

def checkout_mpesa(request):
    cart_data = request.session.get("cart", {})
    items = []
    total = Decimal("0.00")
    for product_id, raw_quantity in cart_data.items():
        try:
            product = Product.objects.get(id=product_id, is_active=True)
            quantity = min(max(0, int(raw_quantity)), product.stock_quantity)
        except (Product.DoesNotExist, TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        subtotal = product.discounted_price * quantity
        total += subtotal
        items.append({"product": product, "quantity": quantity, "subtotal": subtotal, "unit_price": product.discounted_price})

    if request.method != "POST":
        return render(request, "customer_checkout_map.html", {"items": items, "total": total})

    customer_name = request.POST.get("customer_name", "").strip()
    email = request.POST.get("email", "").strip()
    phone = request.POST.get("phone", "").strip()
    address = request.POST.get("address", "").strip()
    delivery_county = request.POST.get("delivery_county", "").strip()
    delivery_town = request.POST.get("delivery_town", "").strip()
    delivery_mode = request.POST.get("delivery_mode", DeliveryTariff.MODE_STANDARD).strip().lower() or DeliveryTariff.MODE_STANDARD
    pickup_point = None
    if delivery_mode == DeliveryTariff.MODE_PICKUP:
        pickup_point_id = request.POST.get("delivery_pickup_point", "").strip()
        try:
            pickup_point = DeliveryPickupPoint.objects.get(id=int(pickup_point_id), is_active=True)
        except (DeliveryPickupPoint.DoesNotExist, TypeError, ValueError):
            return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": "Please select an active Shopiva pickup station."})
        if pickup_point.county.casefold() != delivery_county.casefold():
            return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": "The selected pickup station does not match your county."})
    payment_method = request.POST.get("payment_method", "").strip().lower() or "mpesa"

    if not all([customer_name, email, phone, address]) or not items:
        return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": "Please complete all customer details and make sure your cart is not empty."})
    if payment_method not in {"mpesa", "cod"}:
        return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": "Please select a valid payment method."})
    if payment_method == "mpesa" and not mpesa_production_ready():
        return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": "M-PESA is temporarily unavailable while Safaricom production onboarding is being finalized. Please use Cash on Delivery."})
    if payment_method == "mpesa":
        normalized_payment_phone = normalize_phone(phone)
        if not __import__("re").fullmatch(r"254[17]\d{8}", normalized_payment_phone):
            return render(
                request,
                "customer_checkout_map.html",
                {
                    "items": items,
                    "total": total,
                    "error": "Enter a valid Kenyan M-PESA mobile number, for example 0712345678 or 254712345678.",
                },
            )

    try:
        customer_latitude = Decimal(str(request.POST.get("delivery_latitude", "")).strip())
        customer_longitude = Decimal(str(request.POST.get("delivery_longitude", "")).strip())
    except (InvalidOperation, TypeError, ValueError):
        return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": "Please pin your exact delivery location before continuing."})

    if not (-90 <= customer_latitude <= 90 and -180 <= customer_longitude <= 180):
        return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": "Your delivery map location is invalid. Please pin it again."})

    with transaction.atomic():
        locked_items = []
        for item in items:
            product = Product.objects.select_for_update().get(id=item["product"].id)
            quantity = item["quantity"]
            if not product.is_active or product.stock_quantity < quantity:
                return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": f"Sorry, {product.name} no longer has enough stock."})
            unit_price = product.discounted_price
            locked_items.append((product, quantity, unit_price))

        try:
            quote = calculate_order_quote(
                [(product, quantity) for product, quantity, _ in locked_items],
                customer_latitude,
                customer_longitude,
                delivery_county,
                delivery_town,
                delivery_mode,
            )
        except ValueError as exc:
            return render(request, "customer_checkout_map.html", {"items": items, "total": total, "error": str(exc)})

        final_total = quote["total"]

        # Daraja STK requests use whole Kenyan shillings. Never truncate a
        # fractional total because that would undercharge the order and make
        # callback amount validation fail. For M-PESA, round upward by the
        # smallest amount necessary and keep that adjustment inside Shopiva's
        # service-fee accounting so the order remains balanced.
        mpesa_rounding_adjustment = Decimal("0.00")
        if payment_method == "mpesa":
            mpesa_total = final_total.quantize(Decimal("1"), rounding=ROUND_UP)
            mpesa_rounding_adjustment = mpesa_total - final_total
            if mpesa_rounding_adjustment > 0:
                quote["commission"] = (
                    quote["commission"] + mpesa_rounding_adjustment
                ).quantize(Decimal("0.01"))
                final_total = mpesa_total

        order = Order.objects.create(
            customer_name=customer_name,
            customer=request.user if request.user.is_authenticated and not request.user.is_staff and not hasattr(request.user, "seller_profile") and not hasattr(request.user, "delivery_agent_profile") else None,
            email=email,
            phone=phone,
            address=address,
            total_amount=final_total,
            items_subtotal=quote["subtotal"],
            platform_commission_amount=quote["commission"],
            delivery_fee=quote["delivery_fee"],
            delivery_county=quote["county"],
            delivery_town=quote["destination"],
            delivery_mode=quote["delivery_mode"],
            delivery_distance_km=quote["distance_km"] or Decimal("0.00"),
            delivery_distance_source=quote["distance_source"],
            delivery_hub=quote["hub"],
            delivery_pricing_profile=quote["pricing_profile"],
            delivery_pricing_basis=quote["pricing_basis"],
            delivery_base_fee=quote["base_fee"],
            delivery_distance_rate=quote["distance_rate"],
            delivery_distance_charge=quote["distance_charge"],
            delivery_package_class=quote["package_class"],
            delivery_route_class=quote["route_class"],
            delivery_rate_card=quote["rate_card"],
            delivery_pickup_point=pickup_point,
            status="pending",
            payment_status="unpaid",
            tracking_code=f"SPV-{__import__('uuid').uuid4().hex[:10].upper()}",
        )
        OrderEvent.objects.create(order=order, event_type="placed", note="Order placed through Shopiva checkout.", actor=request.user if request.user.is_authenticated else None)
        if request.user.is_authenticated and not request.user.is_staff:
            transaction.on_commit(
                lambda user_id=request.user.id, order_id=order.id, tracking=order.tracking_code: _safe_order_notification(
                    user_id, order_id, tracking
                )
            )
        for product, quantity, unit_price in locked_items:
            seller = product.seller if product.seller_id and product.seller and product.seller.is_active else None
            gross = unit_price * quantity
            commission_percent = get_platform_commission_percent(unit_price)
            commission = (gross * commission_percent / Decimal("100")).quantize(Decimal("0.01")) if seller else Decimal("0.00")
            seller_net = gross
            OrderItem.objects.create(order=order, product=product, quantity=quantity, price=unit_price, seller=seller, seller_gross=gross, platform_commission=commission, seller_net=seller_net)
            product.stock_quantity -= quantity
            product.save(update_fields=["stock_quantity"])

        if payment_method == "cod":
            PaymentTransaction.objects.create(order=order, method="cod", status="pending", provider="shopiva", amount=final_total, phone=phone, idempotency_key=f"COD-{order.id}")
            order.payment_status = "pending"
            order.status = "confirmed"
            order.save(update_fields=["payment_status", "status"])
            OrderEvent.objects.create(order=order, event_type="confirmed", note="Cash on Delivery order accepted.")
            request.session["cart"] = {}
            request.session.modified = True
            return redirect("order_success", order_id=order.id)

        payment = PaymentTransaction.objects.create(order=order, method="mpesa", status="initiated", provider="daraja", amount=final_total, phone=normalize_phone(phone), idempotency_key=f"MPESA-{order.id}")
        order.payment_status = "pending"
        order.save(update_fields=["payment_status"])
        payment_note = "Waiting for M-PESA STK payment."
        if mpesa_rounding_adjustment > 0:
            payment_note += f" Whole-KSh payment adjustment: KSh {mpesa_rounding_adjustment:.2f}."
        OrderEvent.objects.create(order=order, event_type="payment_pending", note=payment_note)

    try:
        initiate_mpesa_stk(order, payment, payment.phone)
    except Exception as exc:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            payment = PaymentTransaction.objects.select_for_update().get(pk=payment.pk)
            payment.status = "failed"
            payment.raw_response = {"error": str(exc)}
            if not payment.inventory_released:
                _release_reserved_inventory(order)
                payment.inventory_released = True
            payment.save(update_fields=["status", "raw_response", "inventory_released", "updated_at"])
            order.payment_status = "failed"
            order.save(update_fields=["payment_status"])
        return render(
            request,
            "customer_checkout_map.html",
            {
                "items": items,
                "total": final_total,
                "error": f"M-PESA could not be started: {exc}",
            },
        )

    request.session["cart"] = {}
    request.session["payment_order_id"] = order.id
    request.session.modified = True
    return redirect("mpesa_waiting", order_id=order.id)


@csrf_exempt
def mpesa_callback(request):
    if request.method != "POST":
        return JsonResponse({"ResultCode": 1, "ResultDesc": "POST required."}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid JSON."}, status=400)

    callback = payload.get("Body", {}).get("stkCallback", {})
    checkout_request_id = str(callback.get("CheckoutRequestID", ""))
    result_code = callback.get("ResultCode")
    result_desc = callback.get("ResultDesc", "")
    payment = PaymentTransaction.objects.filter(checkout_request_id=checkout_request_id, method="mpesa").first()
    if not payment:
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    with transaction.atomic():
        payment = PaymentTransaction.objects.select_for_update().get(pk=payment.pk)
        order = Order.objects.select_for_update().get(pk=payment.order_id)
        payment.raw_response = payload

        if str(result_code) == "0":
            if payment.status == "paid":
                return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

            metadata = {
                item.get("Name"): item.get("Value")
                for item in callback.get("CallbackMetadata", {}).get("Item", [])
                if isinstance(item, dict) and item.get("Name")
            }
            receipt = str(metadata.get("MpesaReceiptNumber") or "").strip()
            callback_amount = metadata.get("Amount")
            callback_phone = normalize_phone(metadata.get("PhoneNumber") or "")
            expected_phone = normalize_phone(payment.phone)
            try:
                amount_matches = Decimal(str(callback_amount)).quantize(Decimal("0.01")) == Decimal(payment.amount).quantize(Decimal("0.01"))
            except Exception:
                amount_matches = False

            # A positive-looking callback is not enough. A payment becomes paid
            # only when the provider supplies the receipt and matches the amount
            # and phone recorded for this transaction.
            if not receipt or not amount_matches or callback_phone != expected_phone:
                payment.status = "pending"
                payment.provider_reference = receipt
                payment.paid_at = None
                payment.raw_response = {
                    **payload,
                    "shopiva_validation": {
                        "receipt_present": bool(receipt),
                        "amount_matches": amount_matches,
                        "phone_matches": callback_phone == expected_phone,
                    },
                }
                payment.save(update_fields=["status", "provider_reference", "paid_at", "raw_response", "updated_at"])
                return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

            payment.status = "paid"
            payment.provider_reference = receipt
            payment.paid_at = timezone.now()
            order.payment_status = "paid"
            order.status = "paid"
            order.payment_reference = receipt
            order.paid_at = timezone.now()
            order.save(update_fields=["payment_status", "status", "payment_reference", "paid_at"])
            OrderEvent.objects.create(order=order, event_type="paid", note=f"M-PESA payment confirmed: {receipt}")
            _create_seller_settlements(order)

            customer_id = None
            seller_ids = []
            if order.email:
                from django.contrib.auth.models import User
                customer = User.objects.filter(id=order.customer_id, is_active=True).first()
                customer_id = customer.id if customer else None
            seller_ids = [item.seller.user_id for item in order.items.select_related("seller", "seller__user") if item.seller_id and item.seller]
            tracking = order.tracking_code
            order_id = order.id
            seller_ids = list(dict.fromkeys(seller_ids))
            transaction.on_commit(
                lambda customer_id=customer_id, seller_ids=seller_ids, tracking=tracking, order_id=order_id, receipt=receipt: _send_payment_notifications(
                    customer_id, seller_ids, tracking, order_id, receipt
                )
            )
        else:
            payment.status = "failed"
            if not payment.inventory_released:
                _release_reserved_inventory(order)
                payment.inventory_released = True
            order.payment_status = "failed"
            order.save(update_fields=["payment_status"])
            OrderEvent.objects.create(order=order, event_type="cancelled", note=f"M-PESA payment failed: {result_desc}")

        payment.save(update_fields=["status", "provider_reference", "paid_at", "raw_response", "inventory_released", "updated_at"])

    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


def _send_payment_notifications(customer_id, seller_ids, tracking, order_id, receipt):
    from django.contrib.auth.models import User
    if customer_id:
        customer = User.objects.filter(id=customer_id).first()
        if customer:
            notify_user(
                customer,
                "M-PESA payment confirmed",
                f"Your Shopiva order {tracking} is confirmed. M-PESA receipt: {receipt}",
                "payment",
                f"/account/orders/{order_id}/",
            )
    for seller_id in seller_ids:
        seller_user = User.objects.filter(id=seller_id, is_active=True).first()
        if seller_user:
            notify_user(
                seller_user,
                "Order payment confirmed",
                f"Payment for order {tracking} is confirmed. Your seller earnings are now pending delivery.",
                "payment",
                "/seller/",
            )


def _release_reserved_inventory(order):
    for item in order.items.select_related("product").select_for_update():
        product = item.product
        product.stock_quantity += item.quantity
        product.save(update_fields=["stock_quantity"])


def _create_seller_settlements(order):
    seller_totals = {}
    for item in order.items.select_related("seller"):
        if not item.seller_id or not item.seller or not item.seller.is_active:
            continue
        data = seller_totals.setdefault(
            item.seller_id,
            {"seller": item.seller, "gross": Decimal("0.00"), "commission": Decimal("0.00"), "net": Decimal("0.00")},
        )
        data["gross"] += item.seller_gross
        data["commission"] += item.platform_commission
        data["net"] += item.seller_net

    for data in seller_totals.values():
        settlement, created = SellerSettlement.objects.get_or_create(
            order=order,
            seller=data["seller"],
            defaults={
                "gross_amount": data["gross"],
                "platform_commission": data["commission"],
                "seller_amount": data["net"],
                "status": "pending",
            },
        )
        if created:
            wallet, _ = SellerWallet.objects.get_or_create(seller=data["seller"])
            wallet.pending_balance += data["net"]
            wallet.total_sales += data["gross"]
            wallet.total_commission += data["commission"]
            wallet.save(update_fields=["pending_balance", "total_sales", "total_commission", "updated_at"])



def card_payment_success(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    allowed = request.session.get("payment_order_id") == order.id or (request.user.is_authenticated and (request.user.is_staff or order.customer_id == request.user.id))
    if not allowed:
        return JsonResponse({"ok": False, "error": "You are not authorized to view this payment."}, status=403)
    request.session["payment_order_id"] = order.id
    request.session.modified = True
    return render(request, "card_payment_result.html", {"order": order, "success": order.payment_status == "paid"})


def card_payment_cancel(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    allowed = request.session.get("payment_order_id") == order.id or (request.user.is_authenticated and (request.user.is_staff or order.email.lower() == request.user.email.lower()))
    if not allowed:
        return JsonResponse({"ok": False, "error": "You are not authorized to cancel this payment."}, status=403)
    _cancel_card_order(order)
    return redirect("order_success", order_id=order.id)

@csrf_exempt
def stripe_webhook(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    secret = _stripe_webhook_secret()
    if not secret or not _verify_stripe_signature(request.body, request.headers.get("Stripe-Signature", ""), secret):
        return JsonResponse({"ok": False, "error": "Invalid webhook signature."}, status=400)
    try:
        event = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
    if event.get("type") == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        metadata = session.get("metadata", {})
        payment_id = metadata.get("payment_id")
        if payment_id:
            try:
                _mark_card_paid(int(payment_id), session)
            except (PaymentTransaction.DoesNotExist, Order.DoesNotExist, ValueError):
                return JsonResponse({"ok": False, "error": "Payment transaction not found."}, status=404)
    return JsonResponse({"received": True})


def mpesa_payment_verify(request, order_id):
    """Manual STK Query fallback. A successful query still waits for callback receipt validation."""
    order = get_object_or_404(Order, id=order_id)
    allowed = bool(
        request.session.get("payment_order_id") == order.id
        or (request.user.is_authenticated and (request.user.is_staff or order.email.lower() == request.user.email.lower()))
    )
    if not allowed:
        return JsonResponse({"ok": False, "error": "You are not authorized to verify this payment."}, status=403)

    payment = order.payments.filter(method="mpesa").order_by("-created_at").first()
    if not payment:
        return JsonResponse({"ok": False, "error": "No M-PESA payment was found for this order."}, status=404)
    if payment.status == "paid" or order.payment_status == "paid":
        return JsonResponse({"ok": True, "payment_status": "paid", "transaction_status": "paid", "message": "Payment is already confirmed."})

    try:
        provider = query_mpesa_stk(payment)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)

    with transaction.atomic():
        payment = PaymentTransaction.objects.select_for_update().get(pk=payment.pk)
        order = Order.objects.select_for_update().get(pk=order.pk)
        payment.raw_response = {**(payment.raw_response or {}), "last_stk_query": provider}
        result_code = str(provider.get("ResultCode", "")).strip()

        # ResultCode 0 means Daraja processed the request, but the callback is
        # still required before Shopiva marks the order paid because only the
        # callback carries the receipt/amount/phone metadata we validate.
        if result_code == "0":
            payment.status = "pending"
            payment.save(update_fields=["status", "raw_response", "updated_at"])
            return JsonResponse({
                "ok": True,
                "payment_status": order.payment_status,
                "transaction_status": "provider_success_callback_pending",
                "message": "Safaricom reports the STK request succeeded. Shopiva is waiting for the callback so the receipt, amount and phone can be verified.",
            })

        terminal_codes = {"1", "17", "1001", "1019", "1025", "1032", "1037", "2001", "2028", "2029", "2002"}
        if result_code == "2002":
            payment.status = "failed"
            if not payment.inventory_released:
                _release_reserved_inventory(order)
                payment.inventory_released = True
            order.payment_status = "failed"
            order.save(update_fields=["payment_status"])
            OrderEvent.objects.create(
                order=order,
                event_type="cancelled",
                note="M-PESA merchant configuration rejected by Safaricom (2002): Agent/Store number mismatch.",
            )
            payment.save(update_fields=["status", "raw_response", "inventory_released", "updated_at"])
            return JsonResponse({
                "ok": True,
                "payment_status": "failed",
                "transaction_status": "failed",
                "message": "Safaricom rejected the merchant configuration. No M-PESA prompt was sent. Please try again after the merchant configuration is corrected.",
            })

        if result_code in terminal_codes:
            payment.status = "failed"
            if not payment.inventory_released:
                _release_reserved_inventory(order)
                payment.inventory_released = True
            order.payment_status = "failed"
            order.save(update_fields=["payment_status"])
            OrderEvent.objects.create(order=order, event_type="cancelled", note=f"M-PESA verification failed: {provider.get('ResultDesc') or result_code}")
            payment.save(update_fields=["status", "raw_response", "inventory_released", "updated_at"])
            return JsonResponse({
                "ok": True,
                "payment_status": "failed",
                "transaction_status": "failed",
                "message": provider.get("ResultDesc") or "M-PESA payment was not completed.",
            })

        payment.status = "pending"
        payment.save(update_fields=["status", "raw_response", "updated_at"])
        return JsonResponse({
            "ok": True,
            "payment_status": order.payment_status,
            "transaction_status": "pending",
            "message": provider.get("ResultDesc") or "M-PESA is still processing the request.",
        })


def mpesa_payment_status(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    allowed = bool(
        request.session.get("payment_order_id") == order.id
        or (request.user.is_authenticated and (request.user.is_staff or order.email.lower() == request.user.email.lower()))
    )
    if not allowed:
        return JsonResponse({"ok": False, "error": "You are not authorized to view this payment."}, status=403)
    payment = order.payments.filter(method="mpesa").order_by("-created_at").first()
    message = "Waiting for M-PESA confirmation."
    if payment:
        raw = payment.raw_response or {}
        callback = raw.get("Body", {}).get("stkCallback", {}) if isinstance(raw, dict) else {}
        result_code = str(callback.get("ResultCode", "")).strip()
        if payment.status == "failed" and result_code == "2002":
            message = "Safaricom rejected the merchant configuration. No M-PESA prompt was sent. The approved production Till/Store number and Daraja merchant profile must be reconciled before another payment attempt."
        elif payment.status == "failed":
            message = str(callback.get("ResultDesc") or raw.get("error") or "M-PESA payment was not completed.")
        elif payment.status == "paid":
            message = "Payment confirmed."
    return JsonResponse({
        "ok": True,
        "order_id": order.id,
        "payment_status": order.payment_status,
        "order_status": order.status,
        "reference": order.payment_reference,
        "transaction_status": payment.status if payment else None,
        "message": message,
    })


def mpesa_waiting(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    allowed = bool(
        request.session.get("payment_order_id") == order.id
        or (request.user.is_authenticated and (request.user.is_staff or order.email.lower() == request.user.email.lower()))
    )
    if not allowed:
        return JsonResponse({"ok": False, "error": "You are not authorized to view this payment."}, status=403)
    request.session["payment_order_id"] = order.id
    request.session.modified = True
    payment = order.payments.filter(method="mpesa").order_by("-created_at").first()
    return render(request, "mpesa_waiting.html", {"order": order, "payment": payment})
