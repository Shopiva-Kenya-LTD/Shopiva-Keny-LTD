import json
import os
import tempfile
import urllib.request
import uuid

from django.core.cache import cache
from django.http import FileResponse, JsonResponse, HttpResponse
from django.views.decorators.http import require_POST

from .ai import _catalog
from .models import DeliveryAgent, Order, OrderItem, Product, PaymentTransaction, SellerProfile


VOICE_RATE_WINDOW_SECONDS = int(os.getenv("NIA_VOICE_RATE_WINDOW_SECONDS", "60"))
VOICE_RATE_LIMITS = {
    "realtime_call": int(os.getenv("NIA_VOICE_REALTIME_LIMIT", "5")),
    "transcribe_voice": int(os.getenv("NIA_VOICE_TRANSCRIBE_LIMIT", "10")),
    "speak_text": int(os.getenv("NIA_VOICE_SPEAK_LIMIT", "20")),
}


def _voice_auth_error(request):
    """Return a JSON authorization error for browser and API voice callers."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_active:
        return JsonResponse(
            {"ok": False, "error": "Authentication is required for Shopiva voice AI."},
            status=401,
        )
    try:
        delivery_profile = user.delivery_agent_profile
    except DeliveryAgent.DoesNotExist:
        delivery_profile = None
    if delivery_profile is not None and not user.is_staff:
        return JsonResponse(
            {"ok": False, "error": "Voice AI is not available for delivery accounts."},
            status=403,
        )
    return None


def _voice_client_key(request, endpoint):
    user_id = getattr(getattr(request, "user", None), "pk", None) or "unknown"
    ip = request.META.get("REMOTE_ADDR", "unknown")
    return f"nia-voice:{endpoint}:user:{user_id}:ip:{ip}"


def _voice_rate_limit(request, endpoint):
    """Fixed-window throttling for expensive voice endpoints."""
    limit = max(1, VOICE_RATE_LIMITS[endpoint])
    window = max(1, VOICE_RATE_WINDOW_SECONDS)
    key = _voice_client_key(request, endpoint)
    current = cache.get(key)
    if current is None:
        if cache.add(key, 1, timeout=window):
            current = 1
        else:
            current = cache.get(key, 1)
    else:
        try:
            current = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=window)
            current = 1

    if int(current) > limit:
        return JsonResponse(
            {
                "ok": False,
                "error": "Voice AI request limit reached. Please wait a moment and try again.",
            },
            status=429,
            headers={"Retry-After": str(window)},
        )
    return None


def _openai_multipart_sdp(sdp, session):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    boundary = "----ShopivaRealtime" + uuid.uuid4().hex
    parts = []
    parts.append(
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"sdp\"; filename=\"offer.sdp\"\r\n"
        "Content-Type: application/sdp\r\n\r\n"
    )
    parts.append(sdp)
    parts.append("\r\n")
    parts.append(
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="session"\r\n'
        "Content-Type: application/json\r\n\r\n"
    )
    parts.append(json.dumps(session))
    parts.append(f"\r\n--{boundary}--\r\n")
    body = "".join(parts).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/calls",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")


def _catalog_context():
    return [
        {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "description": p.description[:350],
            "price": str(p.discounted_price),
            "original_price": str(p.price),
            "discount_percent": p.discount_percent,
            "stock": p.stock_quantity,
        }
        for p in _catalog(30)
    ]


def _product_payload(product):
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "description": product.description[:350],
        "price": str(product.discounted_price),
        "original_price": str(product.price),
        "discount_percent": product.discount_percent,
        "stock": product.stock_quantity,
    }


def _search_products(query="", category="", max_price=None):
    products = _catalog(120)
    words = {w.lower() for w in query.split() if len(w) > 2}
    ranked = []
    for product in products:
        haystack = f"{product.name} {product.category} {product.description} {product.promo_text}".lower()
        score = sum(2 if w in product.name.lower() else 1 for w in words if w in haystack)
        if category and category.lower() not in product.category.lower():
            continue
        if max_price is not None and product.discounted_price > max_price:
            continue
        if score or not words:
            ranked.append((score, product))
    ranked.sort(key=lambda item: (-item[0], -item[1].discount_percent, item[1].discounted_price))
    return [_product_payload(p) for _, p in ranked[:8]]


def _customer_instructions(request):
    catalog = _catalog_context()
    customer = "Guest customer. You may help them browse and add available products to their session cart."
    if request.user.is_authenticated and not request.user.is_staff:
        orders = list(
            Order.objects.filter(email__iexact=request.user.email)
            .order_by("-created_at")
            .values("id", "tracking_code", "status", "payment_status", "total_amount")[:8]
        )
        customer = f"Authenticated customer. Their recent orders are: {json.dumps(orders, default=str)}."
    return f"""
You are Nia, Shopiva Kenya's natural voice shopping assistant.
Speak naturally and briefly. Use KSh for prices.
The customer may interrupt you. Listen carefully and continue the conversation.
You can help discover products, compare options, explain discounts, add products to the cart, and check the customer's own order status.
Never invent products, prices, stock, discounts, order status, or payment results.
Only recommend products from the catalogue below.
If the customer says "the second one", "that one", or similar, use the products you just discussed.
Never claim an M-PESA payment is successful unless the recorded payment status is exactly paid.
{customer}
CATALOG:
{json.dumps(catalog, ensure_ascii=False)}
"""


def _seller_for_request(request):
    if not request.user.is_authenticated:
        return None
    seller = getattr(request.user, "seller_profile", None)
    return seller if seller and seller.is_active else None


def _seller_instructions(request, seller):
    products = list(
        Product.objects.filter(seller=seller, is_active=True)
        .order_by("-id")
        .values("id", "name", "category", "price", "discount_percent", "stock_quantity")[:40]
    )
    orders = list(
        Order.objects.filter(items__seller=seller).distinct()
        .order_by("-created_at")
        .values("id", "tracking_code", "status", "payment_status", "total_amount")[:30]
    )
    return f"""
You are Nia Seller Voice, the voice operations assistant for the authenticated seller only.
Speak clearly and briefly.
You may help with the seller's own products, stock, orders, sales, commissions and payout balances.
Never reveal data belonging to another seller or administrator.
Never invent a product, stock level, order, payment result, revenue figure or payout balance.
Never claim that you changed a record; current seller tools are read-only.
For payments, only status exactly paid means paid.
Seller products:
{json.dumps(products, default=str, ensure_ascii=False)}
Seller orders:
{json.dumps(orders, default=str, ensure_ascii=False)}
"""
def _admin_instructions():
    catalog = _catalog_context()
    payments = list(
        PaymentTransaction.objects.filter(method="mpesa")
        .order_by("-created_at")
        .values("id", "order_id", "status", "amount", "provider_reference", "created_at")[:20]
    )
    orders = list(
        Order.objects.order_by("-created_at")
        .values("id", "tracking_code", "status", "payment_status", "total_amount", "email")[:20]
    )
    return f"""
You are Nia Admin Voice, the voice operations assistant for Shopiva Kenya.
Speak clearly, concisely and professionally.
Answer using only the supplied Shopiva data.
You can explain products, stock, orders, delivery and M-PESA operations.
Never invent data.
For M-PESA, a payment is successful ONLY when status is exactly paid. Pending is not successful.
M-PESA snapshot:
{json.dumps(payments, default=str)}
Recent orders:
{json.dumps(orders, default=str)}
Product catalogue:
{json.dumps(catalog, ensure_ascii=False)}
"""


def realtime_call(request):
    auth_error = _voice_auth_error(request)
    if auth_error:
        return auth_error
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    rate_limit = _voice_rate_limit(request, "realtime_call")
    if rate_limit:
        return rate_limit
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    if not os.getenv("OPENAI_API_KEY", "").strip():
        return JsonResponse({"ok": False, "error": "Voice AI is not configured yet."}, status=503)

    seller = _seller_for_request(request)
    if request.user.is_authenticated and request.user.is_staff:
        instructions = _admin_instructions()
        tools = [
            {
                "type": "function",
                "name": "get_mpesa_attention",
                "description": "Return M-PESA transactions that are pending or failed. Never treat pending as paid.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_low_stock",
                "description": "Return active products at or below a requested stock threshold.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "threshold": {"type": "integer", "minimum": 0, "maximum": 100}
                    },
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "get_order_attention",
                "description": "Return recent orders with unpaid, pending, or failed payment status that need attention.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]
    elif seller:
        instructions = _seller_instructions(request, seller)
        tools = [
            {
                "type": "function",
                "name": "get_seller_low_stock",
                "description": "Return this seller's active low-stock products.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_seller_orders_attention",
                "description": "Return this seller's own orders needing attention, including unpaid or pending payment orders.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_seller_summary",
                "description": "Return this seller's live product counts, order counts and payout balances.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]
    else:
        instructions = _customer_instructions(request)
        tools = [
            {
                "type": "function",
                "name": "search_products",
                "description": "Search the live Shopiva product catalogue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string"},
                        "max_price": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "get_cart_summary",
                "description": "Read the current customer's real session cart.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_product_details",
                "description": "Return current details for a real Shopiva product.",
                "parameters": {
                    "type": "object",
                    "properties": {"product_id": {"type": "integer"}},
                    "required": ["product_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "add_to_cart",
                "description": "Add a real Shopiva product to the current customer's cart.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "integer"},
                        "quantity": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["product_id", "quantity"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "get_my_order_status",
                "description": "Check the authenticated customer's own order.",
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "integer"}},
                    "required": ["order_id"],
                    "additionalProperties": False,
                },
            },
        ]

    session = {
        "type": "realtime",
        "model": os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "auto",
                }
            },
            "output": {
                "voice": os.getenv("OPENAI_REALTIME_VOICE", "marin"),
            },
        },
        "instructions": instructions,
        "tools": tools,
        "max_output_tokens": 700,
    }

    try:
        offer_sdp = request.body.decode("utf-8")
        answer_sdp = _openai_multipart_sdp(offer_sdp, session)
        return HttpResponse(answer_sdp, content_type="application/sdp")
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Could not start the realtime voice session."},
            status=502,
        )


@require_POST
def realtime_action(request):
    if not request.user.is_authenticated and request.POST.get("action") == "get_my_order_status":
        return JsonResponse({"ok": False, "error": "Please sign in to check your orders."}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST

    action = payload.get("action")
    if action == "search_products":
        max_price = payload.get("max_price")
        try:
            max_price = float(max_price) if max_price not in (None, "") else None
        except (TypeError, ValueError):
            max_price = None
        return JsonResponse({"ok": True, "products": _search_products(
            str(payload.get("query", "")),
            str(payload.get("category", "")),
            max_price,
        )})

    if action == "get_product_details":
        try:
            product_id = int(payload.get("product_id"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid product id."}, status=400)
        product = Product.objects.filter(id=product_id, is_active=True).first()
        if not product:
            return JsonResponse({"ok": False, "error": "That product is not available."}, status=404)
        return JsonResponse({"ok": True, "product": _product_payload(product)})

    if action == "get_cart_summary":
        cart = request.session.get("cart", {})
        items = []
        total = 0
        for product_id, quantity in cart.items():
            try:
                product = Product.objects.get(id=int(product_id), is_active=True)
                qty = max(1, int(quantity))
            except (Product.DoesNotExist, TypeError, ValueError):
                continue
            line_total = product.discounted_price * qty
            total += line_total
            items.append({
                "product_id": product.id,
                "name": product.name,
                "quantity": qty,
                "unit_price": str(product.discounted_price),
                "line_total": str(line_total),
            })
        return JsonResponse({"ok": True, "items": items, "total": str(total)})

    if action == "add_to_cart":
        try:
            product_id = int(payload.get("product_id"))
            quantity = max(1, min(int(payload.get("quantity", 1)), 20))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid product or quantity."}, status=400)

        product = Product.objects.filter(id=product_id, is_active=True).first()
        if not product:
            return JsonResponse({"ok": False, "error": "That product is no longer available."}, status=404)

        cart = request.session.get("cart", {})
        current = int(cart.get(str(product.id), 0))
        new_quantity = min(current + quantity, product.stock_quantity)
        if new_quantity <= current:
            return JsonResponse({"ok": False, "error": f"{product.name} does not have enough stock."}, status=409)

        cart[str(product.id)] = new_quantity
        request.session["cart"] = cart
        request.session.modified = True
        return JsonResponse({"ok": True, "message": f"Added {quantity} {product.name} to your cart.", "product": product.name, "quantity": new_quantity})

    if action == "get_my_order_status":
        try:
            order_id = int(payload.get("order_id"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid order id."}, status=400)

        order = Order.objects.filter(id=order_id, email__iexact=request.user.email).first()
        if not order:
            return JsonResponse({"ok": False, "error": "That order was not found in your account."}, status=404)
        return JsonResponse(
            {
                "ok": True,
                "order_id": order.id,
                "tracking_code": order.tracking_code,
                "status": order.get_status_display(),
                "payment_status": order.get_payment_status_display(),
            }
        )

    if action == "get_seller_low_stock":
        seller = _seller_for_request(request)
        if not seller:
            return JsonResponse({"ok": False, "error": "Active seller access required."}, status=403)
        rows = list(
            Product.objects.filter(seller=seller, is_active=True, stock_quantity__lte=5)
            .order_by("stock_quantity", "name")
            .values("id", "name", "stock_quantity")[:30]
        )
        return JsonResponse({"ok": True, "products": rows})

    if action == "get_seller_orders_attention":
        seller = _seller_for_request(request)
        if not seller:
            return JsonResponse({"ok": False, "error": "Active seller access required."}, status=403)
        rows = list(
            Order.objects.filter(items__seller=seller).distinct()
            .filter(payment_status__in=["unpaid", "pending", "failed"])
            .exclude(status="cancelled")
            .order_by("-created_at")
            .values("id", "tracking_code", "status", "payment_status", "total_amount")[:30]
        )
        return JsonResponse({"ok": True, "orders": rows})

    if action == "get_seller_summary":
        seller = _seller_for_request(request)
        if not seller:
            return JsonResponse({"ok": False, "error": "Active seller access required."}, status=403)
        products = Product.objects.filter(seller=seller, is_active=True)
        orders = Order.objects.filter(items__seller=seller).distinct()
        wallet = getattr(seller, "wallet", None)
        return JsonResponse({
            "ok": True,
            "products": {
                "total": products.count(),
                "low_stock": products.filter(stock_quantity__lte=5).count(),
                "out_of_stock": products.filter(stock_quantity=0).count(),
            },
            "orders": {
                "total": orders.count(),
                "pending": orders.filter(status="pending").count(),
                "paid": orders.filter(payment_status="paid").count(),
            },
            "wallet": {
                "pending_balance": str(wallet.pending_balance) if wallet else "0.00",
                "available_balance": str(wallet.available_balance) if wallet else "0.00",
                "total_sales": str(wallet.total_sales) if wallet else "0.00",
            },
        })

    if action == "get_low_stock":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        try:
            threshold = max(0, min(int(payload.get("threshold", 5)), 100))
        except (TypeError, ValueError):
            threshold = 5
        rows = list(
            Product.objects.filter(is_active=True, stock_quantity__lte=threshold)
            .order_by("stock_quantity", "name")
            .values("id", "name", "stock_quantity", "price", "discount_percent")[:30]
        )
        return JsonResponse({"ok": True, "products": rows})

    if action == "get_order_attention":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        rows = list(
            Order.objects.filter(
                payment_status__in=["unpaid", "pending", "failed"]
            ).exclude(status="cancelled").order_by("-created_at")
            .values("id", "tracking_code", "status", "payment_status", "total_amount", "email", "created_at")[:30]
        )
        return JsonResponse({"ok": True, "orders": rows})

    if action == "get_mpesa_attention":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        rows = list(
            PaymentTransaction.objects.filter(method="mpesa", status__in=["pending", "failed"])
            .order_by("-created_at")
            .values("id", "order_id", "status", "amount", "provider_reference")[:20]
        )
        return JsonResponse({"ok": True, "transactions": rows})

    return JsonResponse({"ok": False, "error": "Unknown voice action."}, status=400)


def transcribe_voice(request):
    auth_error = _voice_auth_error(request)
    if auth_error:
        return auth_error
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    rate_limit = _voice_rate_limit(request, "transcribe_voice")
    if rate_limit:
        return rate_limit
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return JsonResponse({"ok": False, "error": "Voice AI is not configured yet."}, status=503)
    audio = request.FILES.get("audio")
    if not audio:
        return JsonResponse({"ok": False, "error": "No voice recording was received."}, status=400)
    if audio.size > 10 * 1024 * 1024:
        return JsonResponse({"ok": False, "error": "Voice recording is too large. Please keep it under 10 MB."}, status=400)
    suffix = ".webm"
    name = (audio.name or "").lower()
    if "." in name:
        suffix = "." + name.rsplit(".", 1)[-1][:8]
    try:
        from openai import OpenAI
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp:
            for chunk in audio.chunks():
                temp.write(chunk)
            temp.flush()
            with open(temp.name, "rb") as voice_file:
                transcript = OpenAI(api_key=os.getenv("OPENAI_API_KEY")).audio.transcriptions.create(
                    model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
                    file=voice_file,
                )
        return JsonResponse({"ok": True, "text": getattr(transcript, "text", "").strip()})
    except Exception:
        return JsonResponse({"ok": False, "error": "I could not understand that recording. Please try again."}, status=502)


def speak_text(request):
    auth_error = _voice_auth_error(request)
    if auth_error:
        return auth_error
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    rate_limit = _voice_rate_limit(request, "speak_text")
    if rate_limit:
        return rate_limit
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return JsonResponse({"ok": False, "error": "Voice AI is not configured yet."}, status=503)
    text = request.POST.get("text", "").strip()[:2500]
    if not text:
        return JsonResponse({"ok": False, "error": "No text was supplied."}, status=400)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp:
            output_path = temp.name
        speech = client.audio.speech.create(
            model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
            input=text,
            response_format="mp3",
        )
        speech.write_to_file(output_path)
        return FileResponse(open(output_path, "rb"), as_attachment=False, filename="shopiva-ai.mp3", content_type="audio/mpeg")
    except Exception:
        return JsonResponse({"ok": False, "error": "Voice feedback is temporarily unavailable."}, status=502)
