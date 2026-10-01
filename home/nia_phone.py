import logging
import os
import re
import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (
    CustomerAddress,
    DeliveryAgent,
    NiaAuditLog,
    NiaCallSession,
    NiaCallerVerification,
    NiaTask,
    Order,
    Product,
)


logger = logging.getLogger(__name__)
KENYA_PHONE_RE = re.compile(r"^254[17]\d{8}$")


def _env(name, default=""):
    return os.getenv(name, default).strip()


def _public_site_url(request=None):
    configured = _env("PUBLIC_SITE_URL")
    if configured:
        return configured.rstrip("/")
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return "https://shopivakenya.top"


def _normalize_phone(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("254"):
        normalized = digits
    elif digits.startswith("0"):
        normalized = "254" + digits[1:]
    elif digits.startswith("7") or digits.startswith("1"):
        normalized = "254" + digits
    else:
        normalized = digits
    return normalized


def _role_for_user(user):
    if user.is_staff or user.is_superuser:
        return NiaCallSession.ROLE_ADMIN
    seller = getattr(user, "seller_profile", None)
    if seller and seller.is_active:
        return NiaCallSession.ROLE_SELLER
    return NiaCallSession.ROLE_CUSTOMER


def _role_for(request):
    return _role_for_user(request.user)


def _customer_phone(user):
    address = (
        CustomerAddress.objects.filter(user=user, is_default=True)
        .values_list("phone", flat=True)
        .first()
    )
    if address:
        return _normalize_phone(address)
    latest = (
        Order.objects.filter(email__iexact=user.email)
        .exclude(phone="")
        .order_by("-created_at")
        .values_list("phone", flat=True)
        .first()
    )
    return _normalize_phone(latest)


def _outbound_phone_for_user(user):
    role = _role_for_user(user)
    if role == NiaCallSession.ROLE_ADMIN:
        return _normalize_phone(_env("NIA_ADMIN_PHONE"))

    seller = getattr(user, "seller_profile", None)
    if role == NiaCallSession.ROLE_SELLER and seller:
        return _normalize_phone(seller.mpesa_phone)

    return _customer_phone(user)


def _caller_verification_phone_for_user(user):
    role = _role_for_user(user)
    if role == NiaCallSession.ROLE_ADMIN:
        return _normalize_phone(_env("NIA_ADMIN_CALLER_PHONE"))
    if role == NiaCallSession.ROLE_SELLER:
        seller = getattr(user, "seller_profile", None)
        return _normalize_phone(seller.mpesa_phone if seller else "")
    return _customer_phone(user)


def _role_context(request, role):
    if role == NiaCallSession.ROLE_ADMIN:
        recent_orders = list(
            Order.objects.order_by("-created_at")
            .values("id", "tracking_code", "status", "payment_status", "total_amount")[:15]
        )
        return {
            "role_name": "Admin Operations Copilot",
            "summary": {
                "products": Product.objects.count(),
                "pending_orders": Order.objects.filter(status="pending").count(),
                "paid_orders": Order.objects.filter(payment_status="paid").count(),
                "failed_mpesa": Order.objects.filter(payment_status="failed").count(),
                "active_riders": DeliveryAgent.objects.filter(
                    is_active=True, status__in=("available", "on_delivery")
                ).count(),
                "staff_pending": DeliveryAgent.objects.filter(is_active=False).count(),
                "recent_orders": recent_orders,
            },
        }

    seller = getattr(request.user, "seller_profile", None)
    if role == NiaCallSession.ROLE_SELLER and seller:
        products = list(
            Product.objects.filter(seller=seller, is_active=True)
            .values("id", "name", "category", "price", "discount_percent", "stock_quantity")[:40]
        )
        orders = list(
            Order.objects.filter(items__seller=seller).distinct()
            .order_by("-created_at")
            .values("id", "tracking_code", "status", "payment_status", "total_amount")[:20]
        )
        wallet = getattr(seller, "wallet", None)
        return {
            "role_name": "Seller Copilot",
            "summary": {
                "products": products,
                "orders": orders,
                "pending_balance": str(wallet.pending_balance) if wallet else "0.00",
                "available_balance": str(wallet.available_balance) if wallet else "0.00",
                "total_sales": str(wallet.total_sales) if wallet else "0.00",
            },
        }

    orders = list(
        Order.objects.filter(email__iexact=request.user.email)
        .order_by("-created_at")
        .values("id", "tracking_code", "status", "payment_status", "total_amount")[:10]
    )
    cart = request.session.get("cart", {})
    cart_rows = []
    for product_id, quantity in cart.items():
        try:
            product = Product.objects.get(id=int(product_id), is_active=True)
            cart_rows.append(
                {
                    "id": product.id,
                    "name": product.name,
                    "quantity": int(quantity),
                    "price": str(product.discounted_price),
                }
            )
        except (Product.DoesNotExist, TypeError, ValueError):
            continue
    return {
        "role_name": "Shopping Copilot",
        "summary": {"recent_orders": orders, "cart": cart_rows},
    }


def _opening_text(context, task_instruction=""):
    role = context["role_name"]
    if role == "Admin Operations Copilot":
        base = (
            "Hi. This is Nia, your Shopiva Operations Copilot. "
            "I can brief you on orders, payments, delivery, staff approvals and stock."
        )
        return base + (" I am calling for a scheduled Shopiva task briefing." if task_instruction else "") + " What would you like me to check?"
    if role == "Seller Copilot":
        base = (
            "Hi. This is Nia, your Shopiva Seller Copilot. "
            "I can help with your stock, orders, sales and payout balance."
        )
        return base + (" I am calling for a scheduled Shopiva task briefing." if task_instruction else "") + " What would you like me to check?"
    base = (
        "Hi. This is Nia, your Shopiva Shopping Copilot. "
        "I can help with your Shopiva orders and shopping."
    )
    return base + (" I am calling for a scheduled Shopiva task briefing." if task_instruction else "") + " What can I help you with?"


def _log_audit(user, role, action, detail=None):
    try:
        return NiaAuditLog.objects.create(
            user=user,
            role=role,
            action=action,
            detail=detail or {},
        )
    except Exception:
        logger.exception("Nia audit logging failed action=%s role=%s", action, role)
        return None


def _ai_reply(session, user_text):
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        return "I'm unable to use the full Nia intelligence service right now, but your call is connected. Please use the Shopiva dashboard for live information."

    from .nia_core import call_nia

    context = _role_context(type("Request", (), {"user": session.user, "session": {}})(), session.role)
    task_context = ""
    for item in session.conversation:
        if item.get("role") == "task" and item.get("text"):
            task_context = str(item["text"])
            break
    result = call_nia(
        context["role_name"],
        {
            "shopiva": context["summary"],
            "authorized_task_goal": task_context,
            "conversation_history": session.conversation[-12:],
        },
        user_text,
        '{"answer": "string"}',
    )
    if result.get("ai"):
        return str((result.get("data") or {}).get("answer") or "I did not get enough information to answer that.")
    return "Nia is temporarily unable to reach the intelligence service. Please use the Shopiva dashboard for live information."


def _twilio_webhook_valid(request):
    token = _env("TWILIO_AUTH_TOKEN")
    signature = request.META.get("HTTP_X_TWILIO_SIGNATURE", "")
    if not token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator

        url = f"{_public_site_url(request)}{request.path}"
        if request.META.get("QUERY_STRING"):
            url += "?" + request.META["QUERY_STRING"]
        return RequestValidator(token).validate(url, request.POST, signature)
    except Exception:
        return False


def _twilio_client():
    try:
        from twilio.rest import Client
    except ImportError as exc:
        raise RuntimeError("Twilio SDK is not installed.") from exc

    account_sid = _env("TWILIO_ACCOUNT_SID")
    api_key = _env("TWILIO_API_KEY")
    api_secret = _env("TWILIO_API_SECRET")
    auth_token = _env("TWILIO_AUTH_TOKEN")

    if not account_sid:
        raise RuntimeError("TWILIO_ACCOUNT_SID is not configured.")
    if api_key and api_secret:
        return Client(api_key, api_secret, account_sid=account_sid)
    if auth_token:
        return Client(account_sid, auth_token)
    raise RuntimeError("Twilio authentication is not configured.")


def _twilio_ready():
    return _env("NIA_PHONE_CALLS_ENABLED", "false").lower() == "true" and bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_FROM_NUMBER")
        and (
            _env("TWILIO_AUTH_TOKEN")
            or (_env("TWILIO_API_KEY") and _env("TWILIO_API_SECRET"))
        )
    )


def _twiml_gather(request, session_id, say_text):
    from twilio.twiml.voice_response import Gather, VoiceResponse

    base = _public_site_url(request)
    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"{base}/ai/phone/respond/{session_id}/",
        method="POST",
        language="en-KE",
        speech_timeout="auto",
        timeout=6,
    )
    gather.say(say_text)
    response.append(gather)
    response.say("I didn't hear anything. Goodbye.")
    response.hangup()
    return HttpResponse(str(response), content_type="application/xml")


def _twiml_pin_gather(request, session_id, prompt):
    from twilio.twiml.voice_response import Gather, VoiceResponse

    base = _public_site_url(request)
    response = VoiceResponse()
    gather = Gather(
        input="dtmf",
        action=f"{base}/ai/phone/verify/{session_id}/",
        method="POST",
        num_digits=4,
        timeout=10,
    )
    gather.say(prompt)
    response.append(gather)
    response.say("I did not receive the four digit PIN. Goodbye.")
    response.hangup()
    return HttpResponse(str(response), content_type="application/xml")


def issue_caller_pin(user):
    if not user or not user.is_authenticated if hasattr(user, "is_authenticated") else not user:
        raise RuntimeError("Authenticated Shopiva account required.")

    role = _role_for_user(user)
    phone = _caller_verification_phone_for_user(user)
    if not KENYA_PHONE_RE.fullmatch(phone):
        raise RuntimeError("Add a valid Kenyan phone number to your Shopiva profile before generating a Nia call PIN.")

    now = timezone.now()
    NiaCallerVerification.objects.filter(
        user=user,
        used_at__isnull=True,
        expires_at__gt=now,
    ).update(used_at=now)

    pin = f"{secrets.randbelow(10000):04d}"
    verification = NiaCallerVerification.objects.create(
        user=user,
        role=role,
        phone_e164=phone,
        pin_code=make_password(pin),
        expires_at=now + timezone.timedelta(minutes=10),
    )
    # Keep the raw PIN only in memory for the immediate authenticated response;
    # never persist or log it.
    verification._raw_pin = pin
    _log_audit(
        user,
        role,
        "caller_pin_issued",
        {"phone_e164": phone, "expires_at": verification.expires_at.isoformat()},
    )
    return verification


@require_POST
def nia_phone_pin(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Please sign in first."}, status=401)
    try:
        verification = issue_caller_pin(request.user)
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse(
        {
            "ok": True,
            "pin": getattr(verification, "_raw_pin", ""),
            "expires_at": verification.expires_at.isoformat(),
            "role": verification.role,
        }
    )


def place_nia_call_for_user(user, task_instruction=""):
    if not _twilio_ready():
        raise RuntimeError("Nia phone calls are not configured yet.")

    phone = _outbound_phone_for_user(user)
    if not KENYA_PHONE_RE.fullmatch(phone):
        raise RuntimeError("Add a valid Kenyan mobile number to the Shopiva profile/default address first.")

    role = _role_for_user(user)
    conversation = []
    if task_instruction:
        conversation.append({"role": "task", "text": str(task_instruction)[:2000]})
    session = NiaCallSession.objects.create(
        user=user,
        role=role,
        direction=NiaCallSession.DIRECTION_OUTBOUND,
        caller_verified=True,
        phone_e164=phone,
        conversation=conversation,
    )
    base = _public_site_url()
    client = _twilio_client()
    try:
        call = client.calls.create(
            to="+" + phone,
            from_=_env("TWILIO_FROM_NUMBER"),
            url=f"{base}/ai/phone/answer/{session.id}/",
            method="POST",
            status_callback=f"{base}/ai/phone/status/{session.id}/",
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
    except Exception as exc:
        session.status = NiaCallSession.STATUS_FAILED
        code = getattr(exc, "code", None)
        twilio_status = getattr(exc, "status", None)
        detail = getattr(exc, "msg", None) or str(exc)
        session.last_ai_text = "Twilio could not create the outbound call."
        session.save(update_fields=["status", "last_ai_text", "updated_at"])
        _log_audit(
            user,
            role,
            "phone_call_failed",
            {"direction": "outbound", "provider_code": str(code or ""), "provider_status": str(twilio_status or "")},
        )
        logger.exception(
            "Nia outbound call failed role=%s user_id=%s destination=%s twilio_status=%s twilio_code=%s detail=%s",
            role,
            getattr(user, "pk", None),
            phone,
            twilio_status,
            code,
            detail[:500],
        )
        raise

    session.provider_sid = call.sid
    session.status = NiaCallSession.STATUS_QUEUED
    session.save(update_fields=["provider_sid", "status", "updated_at"])
    _log_audit(
        user,
        role,
        "phone_call_placed",
        {"direction": "outbound", "session_id": str(session.id), "provider_sid": call.sid},
    )
    return session


@require_POST
def start_nia_call(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Please sign in before asking Nia to call you."}, status=401)
    lock_key = f"nia-call-lock:{request.user.pk}"
    if not cache.add(lock_key, "1", timeout=60):
        return JsonResponse({"ok": False, "error": "Nia is already placing a call for you. Please wait a moment."}, status=429)
    try:
        session = place_nia_call_for_user(request.user)
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    except Exception as exc:
        code = getattr(exc, "code", None)
        twilio_status = getattr(exc, "status", None)
        if code or twilio_status:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Twilio rejected the call request. Verify the destination number and current Twilio trial/Voice permissions.",
                    "provider_code": str(code) if code else "",
                },
                status=502,
            )
        return JsonResponse({"ok": False, "error": "Nia could not place the phone call right now. Please try again."}, status=502)
    finally:
        cache.delete(lock_key)
    return JsonResponse({"ok": True, "session_id": str(session.id), "status": session.status})


@csrf_exempt
def nia_phone_incoming(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _twilio_webhook_valid(request):
        return HttpResponse("Forbidden", status=403)

    phone = _normalize_phone(request.POST.get("From", ""))
    if not KENYA_PHONE_RE.fullmatch(phone):
        return HttpResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>For your security, I cannot verify this caller. Goodbye.</Say><Hangup/></Response>',
            content_type="application/xml",
        )

    verification = (
        NiaCallerVerification.objects.select_related("user")
        .filter(
            phone_e164=phone,
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
            attempts__lt=5,
        )
        .order_by("-created_at")
        .first()
    )
    from twilio.twiml.voice_response import VoiceResponse

    if not verification or not verification.user_id:
        response = VoiceResponse()
        response.say("I could not find an active Nia call PIN for this number. Open your Shopiva dashboard, generate a new four digit PIN, then call Nia again.")
        response.hangup()
        return HttpResponse(str(response), content_type="application/xml")

    session = NiaCallSession.objects.create(
        user=verification.user,
        role=verification.role,
        direction=NiaCallSession.DIRECTION_INBOUND,
        caller_verified=False,
        verification=verification,
        phone_e164=phone,
        provider_sid=request.POST.get("CallSid", ""),
        status=NiaCallSession.STATUS_IN_PROGRESS,
        conversation=[],
    )
    _log_audit(
        verification.user,
        verification.role,
        "phone_inbound_challenge",
        {"session_id": str(session.id), "provider_sid": session.provider_sid},
    )
    return _twiml_pin_gather(
        request,
        session.id,
        "For your security, please enter the four digit Nia PIN shown in your Shopiva dashboard.",
    )


@csrf_exempt
def nia_phone_verify(request, session_id):
    session = get_object_or_404(NiaCallSession.objects.select_related("user", "verification"), id=session_id)
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _twilio_webhook_valid(request):
        return HttpResponse("Forbidden", status=403)
    if session.direction != NiaCallSession.DIRECTION_INBOUND or session.caller_verified:
        return HttpResponse("Forbidden", status=403)

    verification = session.verification
    digits = re.sub(r"\D", "", str(request.POST.get("Digits", "")))
    if not verification or verification.used_at or verification.expires_at <= timezone.now() or verification.attempts >= 5:
        from twilio.twiml.voice_response import VoiceResponse
        response = VoiceResponse()
        response.say("This Nia verification has expired. Please generate a new PIN from your Shopiva dashboard.")
        response.hangup()
        return HttpResponse(str(response), content_type="application/xml")

    if len(digits) != 4 or not check_password(digits, verification.pin_code):
        verification.attempts += 1
        if verification.attempts >= 5:
            verification.used_at = timezone.now()
        verification.save(update_fields=["attempts", "used_at", "updated_at"])
        _log_audit(
            session.user,
            session.role,
            "phone_pin_failed",
            {"session_id": str(session.id), "attempts": verification.attempts},
        )
        if verification.attempts >= 5:
            from twilio.twiml.voice_response import VoiceResponse
            response = VoiceResponse()
            response.say("Too many incorrect PIN attempts. For your security, please generate a new PIN from Shopiva.")
            response.hangup()
            return HttpResponse(str(response), content_type="application/xml")
        return _twiml_pin_gather(
            request,
            session.id,
            "That PIN is not correct. Please enter the four digit Nia PIN from your Shopiva dashboard.",
        )

    verification.verified_at = timezone.now()
    verification.used_at = timezone.now()
    verification.save(update_fields=["verified_at", "used_at", "updated_at"])
    session.caller_verified = True
    session.save(update_fields=["caller_verified", "updated_at"])
    _log_audit(
        session.user,
        session.role,
        "phone_caller_verified",
        {"session_id": str(session.id)},
    )

    context = _role_context(type("Request", (), {"user": session.user, "session": {}})(), session.role)
    text = _opening_text(context)
    session.conversation.append({"role": "assistant", "text": text, "at": timezone.now().isoformat()})
    session.last_ai_text = text
    session.save(update_fields=["conversation", "last_ai_text", "updated_at"])
    return _twiml_gather(request, session.id, text)


@csrf_exempt
def nia_phone_answer(request, session_id):
    session = get_object_or_404(NiaCallSession, id=session_id)
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _twilio_webhook_valid(request):
        return HttpResponse("Forbidden", status=403)
    if session.direction == NiaCallSession.DIRECTION_INBOUND and not session.caller_verified:
        return HttpResponse("Forbidden", status=403)

    context = _role_context(type("Request", (), {"user": session.user, "session": {}})(), session.role)
    text = _opening_text(context)
    session.conversation.append({"role": "assistant", "text": text, "at": timezone.now().isoformat()})
    session.last_ai_text = text
    session.status = NiaCallSession.STATUS_IN_PROGRESS
    session.save(update_fields=["conversation", "last_ai_text", "status", "updated_at"])
    return _twiml_gather(request, session.id, text)


@csrf_exempt
def nia_phone_respond(request, session_id):
    session = get_object_or_404(NiaCallSession, id=session_id)
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _twilio_webhook_valid(request):
        return HttpResponse("Forbidden", status=403)
    if session.direction == NiaCallSession.DIRECTION_INBOUND and not session.caller_verified:
        return HttpResponse("Forbidden", status=403)
    user_text = str(request.POST.get("SpeechResult", "")).strip()
    if not user_text:
        return _twiml_gather(request, session.id, "I didn't catch that. Please tell me what you need.")
    reply = _ai_reply(session, user_text)
    session.last_user_text = user_text
    session.last_ai_text = reply
    session.conversation.extend(
        [
            {"role": "user", "text": user_text, "at": timezone.now().isoformat()},
            {"role": "assistant", "text": reply, "at": timezone.now().isoformat()},
        ]
    )
    session.save(update_fields=["last_user_text", "last_ai_text", "conversation", "updated_at"])
    return _twiml_gather(request, session.id, reply)


@csrf_exempt
def nia_phone_status(request, session_id):
    session = get_object_or_404(NiaCallSession, id=session_id)
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _twilio_webhook_valid(request):
        return HttpResponse("Forbidden", status=403)
    status = request.POST.get("CallStatus", "").strip().lower()
    mapping = {
        "queued": NiaCallSession.STATUS_QUEUED,
        "initiated": NiaCallSession.STATUS_QUEUED,
        "ringing": NiaCallSession.STATUS_RINGING,
        "in-progress": NiaCallSession.STATUS_IN_PROGRESS,
        "completed": NiaCallSession.STATUS_COMPLETED,
        "failed": NiaCallSession.STATUS_FAILED,
        "busy": NiaCallSession.STATUS_FAILED,
        "no-answer": NiaCallSession.STATUS_NO_ANSWER,
        "canceled": NiaCallSession.STATUS_CANCELED,
    }
    new_status = mapping.get(status)
    if new_status:
        session.status = new_status
        session.save(update_fields=["status", "updated_at"])
        _log_audit(
            session.user,
            session.role,
            "phone_call_status",
            {"session_id": str(session.id), "direction": session.direction, "status": new_status},
        )
    return HttpResponse("OK")
