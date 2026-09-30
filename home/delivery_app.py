from decimal import Decimal, InvalidOperation
from datetime import timedelta
import secrets

from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import transaction, IntegrityError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import DeliveryAgent, DeliveryLocationPing, DeliveryPayout, DeliveryWallet, Order, OrderEvent, SellerSettlement, SellerWallet
from .notification_service import notify_user
from .forms import DeliveryRegistrationForm
from .delivery_payouts import (
    get_active_delivery_pay_profile,
    get_or_create_delivery_wallet,
    queue_delivery_payout,
    record_delivery_earning,
    initiate_delivery_payout,
    handle_pesalink_webhook,
)


DELIVERY_CODE_MAX_ATTEMPTS = 5
DELIVERY_CODE_LOCK_MINUTES = 10


def _agent(request):
    try:
        agent = request.user.delivery_agent_profile
    except DeliveryAgent.DoesNotExist:
        return None
    return agent if agent.is_active else None


def _release_seller_settlements(order, now):
    released = []
    for settlement in SellerSettlement.objects.select_for_update().filter(order=order, status="pending"):
        wallet, _ = SellerWallet.objects.get_or_create(seller=settlement.seller)
        wallet = SellerWallet.objects.select_for_update().get(pk=wallet.pk)
        wallet.pending_balance = max(Decimal("0.00"), wallet.pending_balance - settlement.seller_amount)
        wallet.available_balance += settlement.seller_amount
        wallet.save(update_fields=("pending_balance", "available_balance", "updated_at"))
        settlement.status = "available"
        settlement.released_at = now
        settlement.save(update_fields=("status", "released_at"))
        released.append(settlement)
    return released



def delivery_signup(request):
    """Create a delivery-partner application; staff approval is required before deliveries are accessible."""
    if request.user.is_authenticated:
        if _agent(request):
            return redirect("delivery_portal")
        if request.user.is_staff or request.user.is_superuser:
            return redirect("/admin/")

    if request.method == "POST":
        form = DeliveryRegistrationForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    user = form.save()
            except IntegrityError:
                form.add_error("username", "This account could not be created because the username or email already exists.")
            else:
                return render(request, "delivery/signup_success.html", {"username": user.username, "email": user.email})
    else:
        form = DeliveryRegistrationForm()

    return render(request, "delivery/signup.html", {"form": form})

def delivery_login(request):
    if request.user.is_authenticated:
        if _agent(request):
            return redirect("delivery_portal")
        if request.user.is_staff or request.user.is_superuser:
            return redirect("/admin/")
        logout(request)

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        try:
            agent = user.delivery_agent_profile
        except DeliveryAgent.DoesNotExist:
            form.add_error(None, "This account is not registered as a Shopiva delivery partner.")
        else:
            if not agent.is_active:
                form.add_error(
                    None,
                    "Your staff application is registered and awaiting administrator verification.",
                )
            else:
                login(request, user)
                agent.status = "on_delivery" if agent.orders.filter(status="out_for_delivery").exists() else "available"
                agent.save(update_fields=["status"])
                return redirect("delivery_portal")

    return render(request, "delivery/login.html", {"form": form})


@login_required(login_url="delivery_login")
def delivery_logout(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    agent = _agent(request)
    if agent:
        agent.status = "offline"
        agent.save(update_fields=["status"])
    logout(request)
    return redirect("delivery_login")


@login_required(login_url="delivery_login")
def delivery_action(request, order_id):
    agent = _agent(request)
    if not agent:
        return JsonResponse({"ok": False, "error": "Delivery access is not active."}, status=403)
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    action = request.POST.get("action", "").strip().lower()
    transitions = {
        "start": ("out_for_delivery", "out_for_delivery", "Delivery partner started the delivery."),
        "delivered": ("delivered", "delivered", "Delivery handover verified by the customer code."),
    }
    transition = transitions.get(action)
    if not transition:
        return JsonResponse({"ok": False, "error": "Unknown delivery action."}, status=400)

    target_status, event_type, note = transition
    allowed = {
        "start": {"packed", "processing", "shipped", "confirmed", "paid"},
        "delivered": {"out_for_delivery"},
    }
    notifications = []

    with transaction.atomic():
        try:
            order = Order.objects.select_for_update().select_related("customer").get(id=order_id, delivery_agent=agent)
        except Order.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Delivery order not found or not assigned to you."}, status=404)

        if order.status not in allowed[action]:
            return JsonResponse({
                "ok": False,
                "error": f"Order cannot be marked {target_status.replace('_', ' ')} from its current status.",
            }, status=409)

        now = timezone.now()

        if action == "delivered":
            supplied_code = "".join(ch for ch in request.POST.get("code", "").strip() if ch.isdigit())
            if len(supplied_code) != 6:
                return JsonResponse({"ok": False, "error": "Enter the customer's 6-digit delivery code."}, status=400)

            if not order.delivery_confirmation_code:
                order.ensure_delivery_confirmation_code()
                order.save(update_fields=[
                    "delivery_confirmation_code",
                    "delivery_verification_attempts",
                    "delivery_verification_locked_at",
                ])

            if order.delivery_verification_locked_at and now - order.delivery_verification_locked_at < timedelta(minutes=DELIVERY_CODE_LOCK_MINUTES):
                return JsonResponse({"ok": False, "error": "Delivery verification is temporarily locked after too many failed codes."}, status=429)

            if order.delivery_verification_locked_at:
                order.delivery_verification_attempts = 0
                order.delivery_verification_locked_at = None

            if not secrets.compare_digest(supplied_code, order.delivery_confirmation_code):
                order.delivery_verification_attempts += 1
                if order.delivery_verification_attempts >= DELIVERY_CODE_MAX_ATTEMPTS:
                    order.delivery_verification_locked_at = now
                order.save(update_fields=["delivery_verification_attempts", "delivery_verification_locked_at"])
                remaining = max(0, DELIVERY_CODE_MAX_ATTEMPTS - order.delivery_verification_attempts)
                status_code = 429 if order.delivery_verification_locked_at else 400
                return JsonResponse({
                    "ok": False,
                    "error": "Too many invalid delivery codes. Verification is locked for 10 minutes." if status_code == 429 else "Invalid delivery code.",
                    "attempts_remaining": remaining,
                }, status=status_code)

            order.status = "delivered"
            order.delivered_at = now
            order.delivery_verification_attempts = 0
            order.delivery_verification_locked_at = None
            order.save(update_fields=["status", "delivered_at", "delivery_verification_attempts", "delivery_verification_locked_at"])
            OrderEvent.objects.create(order=order, event_type=event_type, note=note, actor=request.user, delivery_agent=agent)

            rider_earning = record_delivery_earning(order, agent, now=now)
            if rider_earning:
                notifications.append((
                    agent.user,
                    "Delivery earnings pending customer receipt",
                    f"Order {order.tracking_code} is marked delivered. Rider commission will be credited only after the customer confirms receipt.",
                    "payout",
                    "/delivery/payouts/",
                    "",
                    agent.phone,
                ))

            released = _release_seller_settlements(order, now)
            for settlement in released:
                notifications.append((
                    settlement.seller.user, "Seller earnings released",
                    f"Order {order.tracking_code} was delivered. KSh {settlement.seller_amount:,.2f} is now available for payout.",
                    "payout", "/seller/", "", "",
                ))
            if order.customer:
                notifications.append((
                    order.customer, "Order delivered",
                    f"Order {order.tracking_code} has been delivered successfully.",
                    "delivery", f"/account/orders/{order.id}/", order.email, order.phone,
                ))
        else:
            order.status = target_status
            order.assigned_at = order.assigned_at or now
            order.ensure_delivery_confirmation_code()
            order.save(update_fields=[
                "status", "assigned_at", "delivery_confirmation_code",
                "delivery_verification_attempts", "delivery_verification_locked_at",
            ])
            OrderEvent.objects.create(order=order, event_type=event_type, note=note, actor=request.user, delivery_agent=agent)
            if order.customer:
                notifications.append((
                    order.customer, "Your order is out for delivery",
                    f"Your order {order.tracking_code} is now on the way. Your 6-digit delivery handover code is {order.delivery_confirmation_code}. Share it only at handover.",
                    "delivery", f"/account/orders/{order.id}/", order.email, order.phone,
                ))

        agent.status = "available" if target_status == "delivered" else "on_delivery"
        agent.save(update_fields=["status"])

    # Rider commission is released only after the customer confirms receipt.
    for user, title, message, notification_type, link, email, phone in notifications:
        notify_user(user, notification_type, title, message, link=link, email=email, phone=phone)

    return JsonResponse({"ok": True, "status": order.get_status_display(), "order_id": order.id})


@login_required(login_url="delivery_login")
def delivery_history(request):
    agent = _agent(request)
    if not agent:
        return render(request, "delivery/not_authorized.html", status=403)
    orders = (
        agent.orders.filter(status="delivered")
        .select_related("customer")
        .prefetch_related("events")
        .order_by("-delivered_at", "-id")[:50]
    )
    return render(request, "delivery/history.html", {"agent": agent, "orders": orders})



@csrf_exempt
def delivery_payout_webhook(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    import json
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid JSON payload."}, status=400)

    expected = os.getenv("INTASEND_WEBHOOK_CHALLENGE", "").strip()
    if expected and str(payload.get("challenge") or "").strip() != expected:
        return JsonResponse({"ok": False, "error": "Invalid webhook challenge."}, status=403)

    payout, status = handle_pesalink_webhook(payload)
    if payout is not None and status == "paid":
        notify_user(
            payout.agent.user,
            "payout",
            "Delivery commission paid",
            f"Shopiva has confirmed bank payout #{payout.id} of KSh {payout.amount:,.2f}. Reference: {payout.provider_reference or 'received'}",
            link="/delivery/payouts/",
            phone=payout.agent.phone,
        )
    elif payout is not None and status == "failed":
        notify_user(
            payout.agent.user,
            "payout",
            "Delivery commission payout failed",
            f"Shopiva could not complete payout #{payout.id} of KSh {payout.amount:,.2f}. {payout.failure_reason or 'Please review your bank payout details.'}",
            link="/delivery/payouts/",
            phone=payout.agent.phone,
        )

    return JsonResponse({"ok": True, "status": status, "payout_id": payout.id if payout else None})


@login_required(login_url="delivery_login")
def delivery_payouts(request):
    agent = _agent(request)
    if not agent:
        return render(request, "delivery/not_authorized.html", status=403)

    with transaction.atomic():
        wallet = get_or_create_delivery_wallet(agent)

    message = ""
    if request.method == "POST":
        action = request.POST.get("action", "").strip().lower()

        if action == "save_bank":
            wallet.bank_name = request.POST.get("bank_name", "").strip()
            wallet.bank_code = request.POST.get("bank_code", "").strip()
            wallet.bank_account_name = request.POST.get("bank_account_name", "").strip()
            wallet.bank_account_number = request.POST.get("bank_account_number", "").strip()
            if not all((wallet.bank_name, wallet.bank_code, wallet.bank_account_name, wallet.bank_account_number)):
                message = "Complete all bank payout details before saving."
            else:
                wallet.save(update_fields=("bank_name", "bank_code", "bank_account_name", "bank_account_number", "updated_at"))
                message = "Bank payout details saved."

        elif action == "request_payout":
            try:
                payout = queue_delivery_payout(agent, automatic=False)
            except ValueError as exc:
                message = str(exc)
            else:
                if payout:
                    payout = initiate_delivery_payout(payout)
                    if payout.status == DeliveryPayout.STATUS_PROCESSING:
                        message = f"Payout #{payout.id} submitted to the automatic bank payout provider: KSh {payout.amount:,.2f}."
                    else:
                        message = f"Payout #{payout.id} queued for bank payout: KSh {payout.amount:,.2f}."
                else:
                    message = "No payout was queued. Check your available commission balance or an existing pending payout."

        wallet = DeliveryWallet.objects.get(agent=agent)

    pay_history = list(
        DeliveryPayout.objects.filter(agent=agent).order_by("-created_at")[:25]
    )
    earnings = list(
        agent.earnings.select_related("order", "pay_profile")
        .order_by("-earned_at")[:50]
    )
    pay_profile = get_active_delivery_pay_profile()
    minimum_payout = pay_profile.minimum_payout
    earning_commission_percent = pay_profile.commission_percent

    return render(
        request,
        "delivery/payouts.html",
        {
            "agent": agent,
            "wallet": wallet,
            "payouts": pay_history,
            "earnings": earnings,
            "minimum_payout": minimum_payout,
            "earning_commission_percent": earning_commission_percent,
            "message": message,
        },
    )

@login_required(login_url="delivery_login")
def delivery_status(request):
    agent = _agent(request)
    if not agent:
        return JsonResponse({"ok": False, "error": "Delivery access is not active."}, status=403)

    orders = (
        agent.orders
        .exclude(status__in=["delivered", "cancelled"])
        .select_related("delivery_agent")
        .order_by("-created_at")
    )
    return JsonResponse({
        "ok": True,
        "agent": {
            "id": agent.id,
            "name": agent.display_name,
            "status": agent.get_status_display(),
            "live": agent.location_is_live,
            "latitude": float(agent.current_latitude) if agent.current_latitude is not None else None,
            "longitude": float(agent.current_longitude) if agent.current_longitude is not None else None,
            "updated": agent.last_location_at.isoformat() if agent.last_location_at else None,
        },
        "orders": [{
            "id": order.id,
            "tracking_code": order.tracking_code,
            "customer_name": order.customer_name,
            "phone": order.phone,
            "address": order.address,
            "latitude": float(order.delivery_latitude) if order.delivery_latitude is not None else None,
            "longitude": float(order.delivery_longitude) if order.delivery_longitude is not None else None,
            "status": order.get_status_display(),
            "raw_status": order.status,
            "total": str(order.total_amount),
        } for order in orders],
    })


@login_required(login_url="delivery_login")
def delivery_update_location(request):
    if request.method != "POST":
        return redirect("delivery_portal")
    agent = _agent(request)
    if not agent:
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
    DeliveryLocationPing.objects.create(
        agent=agent,
        latitude=agent.current_latitude,
        longitude=agent.current_longitude,
    )
    return redirect("delivery_portal")


@login_required(login_url="delivery_login")
def delivery_ping_location(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    agent = _agent(request)
    if not agent:
        return JsonResponse({"ok": False, "error": "Delivery partner access is not active."}, status=403)

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

    latitude = latitude.quantize(Decimal("0.000001"))
    longitude = longitude.quantize(Decimal("0.000001"))

    with transaction.atomic():
        try:
            locked_agent = DeliveryAgent.objects.select_for_update().get(pk=agent.pk, is_active=True)
        except DeliveryAgent.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Delivery partner access is not active."}, status=403)

        latest_ping = locked_agent.location_history.order_by("-recorded_at").first()
        if latest_ping and (timezone.now() - latest_ping.recorded_at).total_seconds() < 3:
            return JsonResponse({"ok": False, "error": "Location update rate limited. Please wait a moment."}, status=429)

        now = timezone.now()
        locked_agent.current_latitude = latitude
        locked_agent.current_longitude = longitude
        locked_agent.last_location_at = now
        locked_agent.status = "on_delivery" if locked_agent.orders.filter(status="out_for_delivery").exists() else "available"
        locked_agent.save(update_fields=["current_latitude", "current_longitude", "last_location_at", "status"])

        DeliveryLocationPing.objects.create(
            agent=locked_agent,
            latitude=latitude,
            longitude=longitude,
            accuracy_meters=accuracy.quantize(Decimal("0.01")) if accuracy is not None else None,
            speed_mps=speed.quantize(Decimal("0.01")) if speed is not None else None,
            heading_degrees=heading.quantize(Decimal("0.01")) if heading is not None else None,
        )

    return JsonResponse({
        "ok": True,
        "updated_at": now.isoformat(),
        "latitude": float(latitude),
        "longitude": float(longitude),
        "status": locked_agent.get_status_display(),
    })
