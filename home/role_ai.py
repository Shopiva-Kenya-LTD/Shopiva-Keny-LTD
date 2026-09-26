import json
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from datetime import timedelta
from django.http import JsonResponse

from .models import Order, OrderItem, Product, SellerProfile, SellerWallet
from .nia_core import call_nia


def seller_profile_for(request):
    if not request.user.is_authenticated:
        return None
    seller = getattr(request.user, "seller_profile", None)
    if seller and seller.is_active:
        return seller
    return None


def _seller_snapshot(seller):
    products = Product.objects.filter(seller=seller)
    items = OrderItem.objects.filter(seller=seller)
    orders = Order.objects.filter(items__seller=seller).distinct()
    wallet = SellerWallet.objects.filter(seller=seller).only(
        "pending_balance", "available_balance", "total_sales", "total_commission"
    ).first()
    pending_balance = wallet.pending_balance if wallet else Decimal("0.00")
    available_balance = wallet.available_balance if wallet else Decimal("0.00")
    total_sales = wallet.total_sales if wallet else Decimal("0.00")
    total_commission = wallet.total_commission if wallet else Decimal("0.00")
    since = timezone.now() - timedelta(days=30)
    restock = []
    for product in products.filter(is_active=True):
        sold = OrderItem.objects.filter(seller=seller, product=product, order__created_at__gte=since, order__payment_status="paid").aggregate(v=Sum("quantity"))["v"] or 0
        if sold:
            daily_velocity = float(sold) / 30.0
            days_left = float(product.stock_quantity) / daily_velocity if daily_velocity else 9999
            if days_left <= 14:
                restock.append({"product": product.name, "stock": product.stock_quantity, "sold_30d": sold, "estimated_days_left": round(days_left,1)})
    restock.sort(key=lambda x:x["estimated_days_left"])

    return {
        "seller": seller.business_name or seller.user.username,
        "products": {
            "total": products.count(),
            "live": products.filter(is_active=True).count(),
            "low_stock": products.filter(is_active=True, stock_quantity__lte=5, stock_quantity__gt=0).count(),
            "out_of_stock": products.filter(is_active=True, stock_quantity=0).count(),
            "units": products.aggregate(total=Sum("stock_quantity"))["total"] or 0,
        },
        "orders": {
            "total": orders.count(),
            "pending": orders.filter(status="pending").count(),
            "paid": orders.filter(payment_status="paid").count(),
            "delivered": orders.filter(status="delivered").count(),
        },
        "wallet": {
            "pending_balance": str(pending_balance),
            "available_balance": str(available_balance),
            "total_sales": str(total_sales),
            "total_commission": str(total_commission),
        },
    }


def _seller_fallback(question, seller):
    q = question.lower()
    products = Product.objects.filter(seller=seller, is_active=True)
    orders = Order.objects.filter(items__seller=seller).distinct()
    wallet = SellerWallet.objects.filter(seller=seller).only(
        "pending_balance", "available_balance", "total_sales", "total_commission"
    ).first()
    pending_balance = wallet.pending_balance if wallet else Decimal("0.00")
    available_balance = wallet.available_balance if wallet else Decimal("0.00")
    total_sales = wallet.total_sales if wallet else Decimal("0.00")
    total_commission = wallet.total_commission if wallet else Decimal("0.00")

    if "restock" in q or "re-stock" in q or "running out" in q:
        since = timezone.now() - timedelta(days=30)
        rows = []
        for product in products:
            sold = OrderItem.objects.filter(seller=seller, product=product, order__created_at__gte=since, order__payment_status="paid").aggregate(v=Sum("quantity"))["v"] or 0
            if sold:
                velocity = float(sold) / 30.0
                days_left = float(product.stock_quantity) / velocity if velocity else 9999
                if days_left <= 14:
                    rows.append((days_left, product.name, product.stock_quantity))
        rows.sort()
        if rows:
            return "Restock watch:\n" + "\n".join(f"• {name}: {stock} left; about {days:.1f} days at the recent paid-order rate." for days,name,stock in rows[:10])
        return "No active product is currently projected to reach zero stock within about 14 days from the last 30 days of paid-order velocity."

    if "low stock" in q or "low-stock" in q:
        rows = products.filter(stock_quantity__lte=5).order_by("stock_quantity", "name")[:10]
        if not rows:
            return "You currently have no live products at or below 5 units of stock."
        return "Your low-stock products:\n" + "\n".join(
            f"• {p.name}: {p.stock_quantity} unit(s)" for p in rows
        )

    if any(word in q for word in ("out of stock", "out-of-stock", "sold out")):
        rows = products.filter(stock_quantity=0).order_by("name")[:10]
        if not rows:
            return "You currently have no live products that are out of stock."
        return "Your out-of-stock products:\n" + "\n".join(f"• {p.name}" for p in rows)

    if "pending order" in q or ("pending" in q and "order" in q):
        return f"You have {orders.filter(status='pending').count()} pending order(s)."

    if "paid order" in q or ("paid" in q and "order" in q):
        return f"You have {orders.filter(payment_status='paid').count()} paid order(s) in your seller order queue."

    if any(word in q for word in ("sales", "revenue", "earnings", "earned")):
        return (
            f"Your recorded sales are KSh {total_sales:,.2f}. "
            f"Your pending seller balance is KSh {pending_balance:,.2f}, "
            f"and your available payout balance is KSh {available_balance:,.2f}."
        )

    if any(word in q for word in ("commission", "fee", "shopiva fee")):
        return f"Shopiva has recorded KSh {total_commission:,.2f} in platform commission against your sales."

    if any(word in q for word in ("product", "listing", "catalog")) and any(
        word in q for word in ("how many", "count", "total", "number")
    ):
        return (
            f"You have {products.count()} active product(s) in your seller catalogue, "
            f"with {products.filter(stock_quantity__lte=5).count()} at or below 5 units."
        )

    if "help" in q or "what can" in q:
        return (
            "I can help with your products, stock, orders, sales, commissions and payout balances. "
            "Examples: “Which products are low stock?” or “How much is available for payout?”"
        )

    return (
        "I can help with your Shopiva seller operations. Try asking about low stock, "
        "pending orders, paid orders, sales, commissions or payout balance."
    )


def seller_assistant(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)
    seller = seller_profile_for(request)
    if not seller:
        return JsonResponse({"ok": False, "error": "Active seller access is required."}, status=403)

    question = request.POST.get("question", "").strip()
    if not question:
        return JsonResponse({"ok": False, "error": "Please ask a seller question."}, status=400)

    fallback = _seller_fallback(question, seller)
    snapshot = _seller_snapshot(seller)
    result = call_nia(
        "Seller Copilot",
        snapshot,
        question,
        '{"answer": "string"}',
    )
    if result.get("ai"):
        data = result.get("data") or {}
        return JsonResponse({
            "ok": True,
            "ai": True,
            "answer": str(data.get("answer") or fallback),
        })

    return JsonResponse({"ok": True, "ai": False, "answer": fallback})
