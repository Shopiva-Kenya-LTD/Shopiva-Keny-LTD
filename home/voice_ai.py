import json
import os
import tempfile
import urllib.error
import urllib.request
import uuid

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone
from django.http import FileResponse, JsonResponse, HttpResponse
from django.views.decorators.http import require_POST

from .ai import _catalog
from .models import DeliveryAgent, DeliveryEarning, Order, OrderItem, Product, PaymentTransaction, SellerProfile, SellerSettlement, SellerWallet, DeliveryWallet


VOICE_RATE_WINDOW_SECONDS = int(os.getenv("NIA_VOICE_RATE_WINDOW_SECONDS", "60"))
VOICE_RATE_LIMITS = {
    "realtime_call": int(os.getenv("NIA_VOICE_REALTIME_LIMIT", "5")),
    "transcribe_voice": int(os.getenv("NIA_VOICE_TRANSCRIBE_LIMIT", "10")),
    "speak_text": int(os.getenv("NIA_VOICE_SPEAK_LIMIT", "20")),
    "realtime_action": int(os.getenv("NIA_VOICE_ACTION_LIMIT", "30")),
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
    # Delivery accounts get a strictly scoped read-only copilot below.
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
        "Content-Disposition: form-data; name=\"sdp\"\r\n"
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
            "Accept": "application/sdp",
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


def _delivery_for_request(request):
    if not request.user.is_authenticated:
        return None
    agent = getattr(request.user, "delivery_agent_profile", None)
    return agent if agent and agent.is_active else None


def _delivery_instructions(request, agent):
    wallet = getattr(agent, "wallet", None)
    current_orders = list(
        agent.orders.exclude(status__in=["delivered", "cancelled"])
        .order_by("-created_at")
        .values(
            "id", "tracking_code", "status", "payment_status",
            "delivery_town", "delivery_county", "delivery_distance_km",
            "delivery_fee", "created_at",
        )[:20]
    )
    return f"""
You are Nia Delivery Copilot for the authenticated Shopiva delivery agent only.
Speak naturally, briefly and operationally.
You may report this agent's assigned deliveries, delivery status, earnings and payout balances.
Never reveal another delivery agent's data, customer secrets, delivery confirmation codes, or private payment credentials.
Never claim a delivery was completed, assigned, cancelled, or paid unless the live Shopiva data says so.
Current assigned deliveries:
{json.dumps(current_orders, default=str, ensure_ascii=False)}
Wallet snapshot:
{json.dumps({
    "available_balance": str(wallet.available_balance) if wallet else "0.00",
    "pending_payout_balance": str(wallet.pending_payout_balance) if wallet else "0.00",
    "total_earned": str(wallet.total_earned) if wallet else "0.00",
    "total_paid": str(wallet.total_paid) if wallet else "0.00",
    "auto_payout_enabled": bool(wallet.auto_payout_enabled) if wallet else False,
}, default=str)}
"""


def _delivery_summary(agent):
    wallet = getattr(agent, "wallet", None)
    active = agent.orders.exclude(status__in=["delivered", "cancelled"])
    earnings = DeliveryEarning.objects.filter(agent=agent)
    return {
        "agent": agent.display_name,
        "status": agent.get_status_display(),
        "location_live": bool(agent.location_is_live),
        "last_location_at": agent.last_location_at.isoformat() if agent.last_location_at else None,
        "active_deliveries": active.count(),
        "available_earnings": str(earnings.filter(status="available").aggregate(total=Sum("total_amount"))["total"] or 0),
        "today_earnings": str(earnings.filter(earned_at__date=timezone.localdate()).exclude(status="reversed").aggregate(total=Sum("total_amount"))["total"] or 0),
        "wallet_available": str(wallet.available_balance) if wallet else "0.00",
        "wallet_pending_payout": str(wallet.pending_payout_balance) if wallet else "0.00",
        "wallet_total_earned": str(wallet.total_earned) if wallet else "0.00",
        "wallet_total_paid": str(wallet.total_paid) if wallet else "0.00",
        "auto_payout_enabled": bool(wallet.auto_payout_enabled) if wallet else False,
    }


def _delivery_orders(agent):
    return list(
        agent.orders.exclude(status__in=["delivered", "cancelled"])
        .order_by("-created_at")
        .values(
            "id", "tracking_code", "status", "payment_status",
            "delivery_town", "delivery_county", "delivery_distance_km",
            "delivery_fee", "created_at",
        )[:30]
    )


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

def _audit_voice_action(request, action, detail=None):
    """Record Nia tool usage without storing raw audio or secrets."""
    try:
        from .models import NiaAuditLog
        role = "admin" if request.user.is_staff else ("seller" if _seller_for_request(request) else "customer")
        NiaAuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            role=role,
            action=action,
            detail=detail or {},
        )
    except Exception:
        pass


def _admin_business_summary():
    today = timezone.localdate()
    orders_today = Order.objects.filter(created_at__date=today)
    paid_today = orders_today.filter(payment_status="paid")
    revenue = paid_today.aggregate(total=Sum("total_amount"))["total"] or 0
    return {
        "date": str(today),
        "orders_today": orders_today.count(),
        "paid_orders_today": paid_today.count(),
        "revenue_today": str(revenue),
        "active_products": Product.objects.filter(is_active=True).count(),
        "low_stock_products": Product.objects.filter(is_active=True, stock_quantity__lte=5).count(),
        "active_sellers": SellerProfile.objects.filter(is_active=True).count(),
        "active_delivery_agents": DeliveryAgent.objects.filter(is_active=True).count(),
        "delivery_available": DeliveryAgent.objects.filter(is_active=True, status="available").count(),
        "delivery_on_delivery": DeliveryAgent.objects.filter(is_active=True, status="on_delivery").count(),
    }


def _admin_delivery_overview():
    rows = []
    for agent in DeliveryAgent.objects.select_related("user").filter(is_active=True).order_by("status", "user__username")[:50]:
        rows.append({
            "name": agent.display_name,
            "status": agent.get_status_display(),
            "phone": agent.phone,
            "vehicle": agent.vehicle_type,
            "vehicle_number": agent.vehicle_number,
            "location_live": agent.location_is_live,
            "last_location_at": agent.last_location_at.isoformat() if agent.last_location_at else None,
            "current_orders": agent.orders.exclude(status="delivered").exclude(status="cancelled").count(),
        })
    return rows


def _admin_seller_overview():
    rows = []
    for seller in SellerProfile.objects.select_related("user").filter(is_active=True).order_by("business_name")[:50]:
        wallet = getattr(seller, "wallet", None)
        rows.append({
            "seller": seller.business_name or seller.user.get_full_name() or seller.user.username,
            "products": seller.products.filter(is_active=True).count(),
            "orders": seller.order_items.values("order_id").distinct().count(),
            "available_balance": str(wallet.available_balance) if wallet else "0.00",
            "pending_balance": str(wallet.pending_balance) if wallet else "0.00",
            "total_sales": str(wallet.total_sales) if wallet else "0.00",
            "commission": str(wallet.total_commission) if wallet else "0.00",
        })
    return rows


def _order_details(order_id, seller=None):
    qs = Order.objects.select_related("delivery_agent", "delivery_hub", "delivery_pickup_point").prefetch_related("items__product", "items__seller")
    order = qs.filter(id=order_id).first()
    if not order or (seller and not order.items.filter(seller=seller).exists()):
        return None
    return {
        "id": order.id,
        "tracking_code": order.tracking_code,
        "customer_name": order.customer_name if seller is None else "Protected customer",
        "status": order.get_status_display(),
        "payment_status": order.get_payment_status_display(),        "total_amount": str(order.total_amount),
        "items_subtotal": str(order.items_subtotal),
        "delivery_fee": str(order.delivery_fee),
        "delivery_town": order.delivery_town,
        "delivery_county": order.delivery_county,
        "delivery_distance_km": str(order.delivery_distance_km),
        "delivery_mode": order.delivery_mode,
        "delivery_agent": order.delivery_agent.display_name if order.delivery_agent else None,
        "created_at": order.created_at.isoformat(),
        "items": [
            {"name": i.product.name, "quantity": i.quantity, "price": str(i.price), "seller": str(i.seller) if i.seller else None}
            for i in order.items.all()
        ],
    }


def _branch_overview():
    from .models import ShopivaBranch, ShopivaOutlet
    return {
        "branches": [
            {"name": b.name, "town": b.town, "county": b.county, "address": b.address, "active": b.is_active, "headquarters": b.is_headquarters}
            for b in ShopivaBranch.objects.filter(is_active=True)[:50]
        ],
        "outlets": [
            {"name": o.name, "town": o.town, "county": o.county, "address": o.address, "pickup_available": o.pickup_available}
            for o in ShopivaOutlet.objects.filter(is_active=True)[:50]
        ],
    }




def _admin_financial_overview():
    paid = PaymentTransaction.objects.filter(status="paid")
    failed = PaymentTransaction.objects.filter(status="failed")
    pending = PaymentTransaction.objects.filter(status__in=["initiated", "pending"])
    return {
        "paid_count": paid.count(),
        "paid_value": str(paid.aggregate(total=Sum("amount"))["total"] or 0),
        "pending_count": pending.count(),
        "pending_value": str(pending.aggregate(total=Sum("amount"))["total"] or 0),
        "failed_count": failed.count(),
        "seller_settlements_pending": SellerSettlement.objects.filter(status="pending").count(),
        "seller_settlements_available": SellerSettlement.objects.filter(status="available").count(),
        "seller_wallet_available": str(SellerWallet.objects.aggregate(total=Sum("available_balance"))["total"] or 0),
        "delivery_wallet_available": str(DeliveryWallet.objects.aggregate(total=Sum("available_balance"))["total"] or 0),
    }


def _admin_attention():
    today = timezone.localdate()
    return {
        "unpaid_orders": Order.objects.filter(payment_status__in=["unpaid", "pending"], status__in=["pending", "confirmed"]).count(),
        "failed_payments": PaymentTransaction.objects.filter(status="failed", created_at__date=today).count(),
        "low_stock": Product.objects.filter(is_active=True, stock_quantity__lte=5).count(),
        "unassigned_paid_orders": Order.objects.filter(payment_status="paid", delivery_agent__isnull=True).exclude(status__in=["delivered", "cancelled"]).count(),
        "active_deliveries": Order.objects.filter(status="out_for_delivery").count(),
        "pending_seller_settlements": SellerSettlement.objects.filter(status="pending").count(),
        "pending_seller_payouts": SellerSettlement.objects.filter(status="available").count(),
    }

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
You have live Shopiva tools. When the user asks for current numbers, status, delivery activity, seller balances, branches, or an order, call the relevant live tool instead of relying on the snapshot.
Never claim to have performed an action unless a server tool actually performed it. For sensitive actions such as payouts, refunds, approvals, deleting records, changing payments, or assigning deliveries, require explicit confirmation before any future write tool is enabled.
If data is unavailable, say so rather than guessing.
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
    delivery_agent = _delivery_for_request(request)
    if request.user.is_authenticated and request.user.is_staff:
        instructions = _admin_instructions()
        tools = [
            {
                "type": "function",
                "name": "get_business_summary",
                "description": "Return a live Shopiva operations summary including today's orders, paid revenue, stock, sellers and delivery agents.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_financial_overview",
                "description": "Return live payment, seller settlement and delivery wallet totals.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_attention_overview",
                "description": "Return live Shopiva items needing attention: unpaid orders, failed payments, low stock, unassigned paid orders and pending settlements.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_delivery_overview",
                "description": "Return live delivery-agent status, vehicles, location freshness and active delivery counts.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_seller_overview",
                "description": "Return live seller-level product, order and wallet summary.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_order_details",
                "description": "Return live details for a specific Shopiva order.",
                "parameters": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"], "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_branch_overview",
                "description": "Return active Shopiva branches and outlets.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
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
    elif delivery_agent:
        instructions = _delivery_instructions(request, delivery_agent)
        tools = [
            {
                "type": "function",
                "name": "get_delivery_summary",
                "description": "Return this authenticated delivery agent's live status, assigned delivery count, earnings and payout balances.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "type": "function",
                "name": "get_my_delivery_orders",
                "description": "Return this authenticated delivery agent's active assigned orders only.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]
    elif seller:
        instructions = _seller_instructions(request, seller)
        tools = [
            {
                "type": "function",
                "name": "get_seller_order_details",
                "description": "Return live details for one of this seller's orders.",
                "parameters": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"], "additionalProperties": False},
            },
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
    except urllib.error.HTTPError as exc:
        # Surface the provider's non-secret error so the browser can distinguish
        # billing/quota/auth/model failures from a local WebRTC failure.
        try:
            provider_body = exc.read().decode("utf-8", errors="replace")
            provider_data = json.loads(provider_body)
            provider_error = provider_data.get("error") or {}
            detail = provider_error.get("message") or provider_error.get("code") or "OpenAI rejected the realtime session."
        except Exception:
            detail = "OpenAI rejected the realtime session."        return JsonResponse(
            {"ok": False, "error": f"Realtime provider error ({exc.code}): {detail}"},
            status=502,
        )
    except Exception as exc:
        return JsonResponse(
            {"ok": False, "error": f"Could not start the realtime voice session: {type(exc).__name__}."},
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
    rate_limit = _voice_rate_limit(request, "realtime_action")
    if rate_limit:
        return rate_limit
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
        _audit_voice_action(request, action, {"product_id": product.id, "quantity": quantity})
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

    if action == "get_delivery_summary":
        agent = _delivery_for_request(request)
        if not agent:
            return JsonResponse({"ok": False, "error": "Active delivery access required."}, status=403)
        data = _delivery_summary(agent)
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, **data})

    if action == "get_my_delivery_orders":
        agent = _delivery_for_request(request)
        if not agent:
            return JsonResponse({"ok": False, "error": "Active delivery access required."}, status=403)
        data = _delivery_orders(agent)
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, "orders": data})

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



    if action == "get_financial_overview":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        data = _admin_financial_overview()
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, **data})

    if action == "get_attention_overview":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        data = _admin_attention()
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, **data})

    if action == "get_business_summary":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        data = _admin_business_summary()
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, **data})

    if action == "get_delivery_overview":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        data = _admin_delivery_overview()
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, "agents": data})

    if action == "get_seller_overview":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        data = _admin_seller_overview()
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, "sellers": data})

    if action == "get_branch_overview":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        data = _branch_overview()
        _audit_voice_action(request, action)
        return JsonResponse({"ok": True, **data})

    if action == "get_order_details":
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "Admin access required."}, status=403)
        try:
            order_id = int(payload.get("order_id"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid order id."}, status=400)
        data = _order_details(order_id)
        if not data:
            return JsonResponse({"ok": False, "error": "That order was not found."}, status=404)
        _audit_voice_action(request, action, {"order_id": order_id})
        return JsonResponse({"ok": True, "order": data})

    if action == "get_seller_order_details":
        seller = _seller_for_request(request)
        if not seller:
            return JsonResponse({"ok": False, "error": "Active seller access required."}, status=403)
        try:
            order_id = int(payload.get("order_id"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Invalid order id."}, status=400)
        data = _order_details(order_id, seller=seller)
        if not data:
            return JsonResponse({"ok": False, "error": "That order was not found for your seller account."}, status=404)
        _audit_voice_action(request, action, {"order_id": order_id})
        return JsonResponse({"ok": True, "order": data})

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