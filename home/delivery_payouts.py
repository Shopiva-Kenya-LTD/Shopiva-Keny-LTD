from decimal import Decimal, ROUND_HALF_UP
import base64
import json
import os
import re
import urllib.error
import urllib.request
import uuid

from django.db import transaction
from django.utils import timezone

from .models import (
    DeliveryAgent,
    DeliveryEarning,
    DeliveryPayProfile,
    DeliveryPayout,
    DeliveryWallet,
)


ZERO = Decimal("0.00")
TWO_PLACES = Decimal("0.01")
DEFAULT_BASE_PER_DELIVERY = Decimal("100.00")
DEFAULT_PER_KM_RATE = Decimal("15.00")
DEFAULT_MINIMUM_PAYOUT = Decimal("500.00")


def _money(value):
    return Decimal(value or ZERO).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def normalize_payout_phone(value):
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    if digits.startswith("7") and len(digits) == 9:
        digits = "254" + digits
    if not digits.startswith("254") or len(digits) != 12 or not digits[3] in "7":
        raise ValueError("Enter a valid Kenyan mobile number.")
    return digits


def get_active_delivery_pay_profile():
    profile = DeliveryPayProfile.objects.filter(is_active=True).order_by("-updated_at", "-id").first()
    if profile:
        return profile
    return DeliveryPayProfile.objects.create(
        name="Shopiva Standard Rider Pay",
        base_per_delivery=DEFAULT_BASE_PER_DELIVERY,
        per_km_rate=DEFAULT_PER_KM_RATE,
        minimum_payout=DEFAULT_MINIMUM_PAYOUT,
        auto_payout_enabled=True,
        auto_payout_threshold=DEFAULT_MINIMUM_PAYOUT,
        is_active=True,
        notes="Default starting schedule. Review against approved Shopiva transport economics before changing.",
    )


def get_or_create_delivery_wallet(agent):
    wallet, _ = DeliveryWallet.objects.get_or_create(
        agent=agent,
        defaults={
            "payout_phone": agent.phone or "",
            "auto_payout_enabled": True,
            "auto_payout_threshold": DEFAULT_MINIMUM_PAYOUT,
        },
    )
    return DeliveryWallet.objects.select_for_update().get(pk=wallet.pk)


def calculate_delivery_earning(order, profile):
    distance_km = max(Decimal("0.00"), _money(order.delivery_distance_km))
    base_amount = _money(profile.base_per_delivery)
    distance_amount = _money(distance_km * profile.per_km_rate)
    total_amount = _money(base_amount + distance_amount)
    return distance_km, base_amount, distance_amount, total_amount


def queue_delivery_payout(agent, *, automatic=False, force=False):
    """Reserve all currently available earnings for one payout.

    No external money movement is performed here. The payout is queued and
    can later be processed by the configured disbursement provider.
    """
    with transaction.atomic():
        wallet = get_or_create_delivery_wallet(agent)
        profile = get_active_delivery_pay_profile()
        minimum = _money(profile.minimum_payout)

        if DeliveryPayout.objects.filter(
            agent=agent, status__in=("queued", "processing")
        ).exists():
            return None

        available = _money(wallet.available_balance)
        if available <= ZERO:
            return None
        if automatic and not (wallet.auto_payout_enabled and available >= _money(wallet.auto_payout_threshold)):
            return None
        if not automatic and not force and available < minimum:
            raise ValueError(f"Minimum payout is KSh {minimum:,.2f}.")

        phone = (wallet.payout_phone or agent.phone or "").strip()
        try:
            phone = normalize_payout_phone(phone)
        except ValueError as exc:
            raise ValueError("Set a valid M-PESA payout phone number before requesting payout.") from exc

        earnings = list(
            DeliveryEarning.objects.select_for_update()
            .filter(agent=agent, status="available", payout__isnull=True)
            .order_by("earned_at", "id")
        )
        selected = []
        running = ZERO
        for earning in earnings:
            selected.append(earning)
            running += _money(earning.total_amount)
            if running >= available:
                break

        amount = _money(running)
        if amount <= ZERO or amount != available:
            raise ValueError("Delivery earnings ledger could not be reconciled. No payout was queued.")

        payout = DeliveryPayout.objects.create(
            agent=agent,
            amount=amount,
            phone=phone,
            status=DeliveryPayout.STATUS_QUEUED,
            trigger=DeliveryPayout.TRIGGER_AUTOMATIC if automatic else DeliveryPayout.TRIGGER_MANUAL,
            idempotency_key=uuid.uuid4().hex,
        )
        for earning in selected:
            earning.payout = payout
            earning.status = DeliveryEarning.STATUS_RESERVED
            earning.save(update_fields=("payout", "status"))
        wallet.available_balance = _money(wallet.available_balance - amount)
        wallet.pending_payout_balance = _money(wallet.pending_payout_balance + amount)
        wallet.save(update_fields=("available_balance", "pending_payout_balance", "updated_at"))
        return payout


def record_delivery_earning(order, agent, now=None):
    """Create the rider's immutable earning once an order is customer-verified delivered."""
    if not agent or not order:
        return None

    with transaction.atomic():
        existing = (
            DeliveryEarning.objects.select_for_update()
            .filter(order=order)
            .first()
        )
        if existing:
            return existing

        profile = get_active_delivery_pay_profile()
        distance_km, base_amount, distance_amount, total_amount = calculate_delivery_earning(order, profile)
        wallet = get_or_create_delivery_wallet(agent)

        earning = DeliveryEarning.objects.create(
            agent=agent,
            order=order,
            distance_km=distance_km,
            distance_source=order.delivery_distance_source or "estimated",
            pay_profile=profile,
            base_amount=base_amount,
            distance_amount=distance_amount,
            total_amount=total_amount,
            status=DeliveryEarning.STATUS_AVAILABLE,
            earned_at=now or timezone.now(),
        )
        wallet.available_balance = _money(wallet.available_balance + total_amount)
        wallet.total_earned = _money(wallet.total_earned + total_amount)
        wallet.save(update_fields=("available_balance", "total_earned", "updated_at"))

        return earning


def mpesa_b2c_ready():
    """Return True only when Shopiva's dedicated rider B2C disbursement configuration is complete."""
    if os.getenv("MPESA_B2C_ENABLED", "false").strip().lower() != "true":
        return False
    if os.getenv("MPESA_ENV", "sandbox").strip().lower() not in {"sandbox", "production"}:
        return False
    required = (
        "MPESA_CONSUMER_KEY",
        "MPESA_CONSUMER_SECRET",
        "MPESA_B2C_INITIATOR_NAME",
        "MPESA_B2C_SECURITY_CREDENTIAL",
        "MPESA_B2C_SHORTCODE",
        "MPESA_B2C_RESULT_URL",
        "MPESA_B2C_TIMEOUT_URL",
    )
    return all(os.getenv(name, "").strip() for name in required)


def _mpesa_b2c_base_url():
    return (
        "https://api.safaricom.co.ke"
        if os.getenv("MPESA_ENV", "sandbox").strip().lower() == "production"
        else "https://sandbox.safaricom.co.ke"
    )


def _mpesa_b2c_request(url, payload, token):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
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


def _daraja_access_token():
    key = os.getenv("MPESA_CONSUMER_KEY", "").strip()
    secret = os.getenv("MPESA_CONSUMER_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("Daraja consumer credentials are not configured.")

    auth = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{_mpesa_b2c_base_url()}/oauth/v1/generate?grant_type=client_credentials",
        method="GET",
        headers={"Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Daraja authentication failed.")
    return token


def initiate_delivery_payout(payout):
    """Submit a queued rider payout to Daraja B2C when explicitly enabled.

    The provider result is asynchronous. A synchronous acceptance moves the
    payout to processing; only the signed/known provider callback may mark it paid.
    Network failures do not release the reserved balance because the provider
    may still have accepted the request.
    """
    if not payout or not mpesa_b2c_ready():
        return payout

    with transaction.atomic():
        locked = DeliveryPayout.objects.select_for_update().get(pk=payout.pk)
        if locked.status == DeliveryPayout.STATUS_PAID:
            return locked
        if locked.status not in (DeliveryPayout.STATUS_QUEUED, DeliveryPayout.STATUS_PROCESSING):
            return locked
        originator_id = locked.idempotency_key
        response_state = dict(locked.provider_response or {})
        response_state["originator_conversation_id"] = originator_id
        locked.status = DeliveryPayout.STATUS_PROCESSING
        locked.provider = "mpesa_b2c"
        locked.provider_response = response_state
        locked.failure_reason = ""
        locked.save(update_fields=("status", "provider", "provider_response", "failure_reason", "updated_at"))

    payload = {
        "OriginatorConversationID": originator_id,
        "InitiatorName": os.getenv("MPESA_B2C_INITIATOR_NAME", "").strip(),
        "SecurityCredential": os.getenv("MPESA_B2C_SECURITY_CREDENTIAL", "").strip(),
        "CommandID": os.getenv("MPESA_B2C_COMMAND_ID", "BusinessPayment").strip() or "BusinessPayment",
        "Amount": int(_money(payout.amount)),
        "PartyA": os.getenv("MPESA_B2C_SHORTCODE", "").strip(),
        "PartyB": payout.phone,
        "Remarks": f"Shopiva delivery payout #{payout.id}",
        "QueueTimeOutURL": os.getenv("MPESA_B2C_TIMEOUT_URL", "").strip(),
        "ResultURL": os.getenv("MPESA_B2C_RESULT_URL", "").strip(),
        "Occasion": f"Rider earnings payout #{payout.id}",
    }
    endpoint = os.getenv(
        "MPESA_B2C_URL",
        f"{_mpesa_b2c_base_url()}/mpesa/b2c/v3/paymentrequest",
    ).strip()

    try:
        token = _daraja_access_token()
        status, response = _mpesa_b2c_request(endpoint, payload, token)
    except Exception as exc:
        with transaction.atomic():
            locked = DeliveryPayout.objects.select_for_update().get(pk=payout.pk)
            state = dict(locked.provider_response or {})
            state["submission_error"] = str(exc)[:500]
            locked.provider_response = state
            locked.save(update_fields=("provider_response", "updated_at"))
            return locked

    with transaction.atomic():
        locked = DeliveryPayout.objects.select_for_update().get(pk=payout.pk)
        state = dict(locked.provider_response or {})
        state["submission_response"] = response

        response_code = str(response.get("ResponseCode", "")).strip()
        accepted = status in (200, 201) and response_code in {"0", ""}
        if accepted:
            locked.status = DeliveryPayout.STATUS_PROCESSING
            provider_id = (
                response.get("OriginatorConversationID")
                or response.get("ConversationID")
                or originator_id
            )
            locked.provider_reference = str(provider_id)
            state["originator_conversation_id"] = originator_id
            locked.provider_response = state
            locked.failure_reason = ""
            locked.save(update_fields=("status", "provider_reference", "provider_response", "failure_reason", "updated_at"))
            return locked

        locked.status = DeliveryPayout.STATUS_FAILED
        locked.failure_reason = str(
            response.get("errorMessage")
            or response.get("ResponseDescription")
            or response.get("error")
            or f"Daraja B2C rejected the request (HTTP {status})."
        )[:255]
        locked.provider_response = state
        locked.processed_at = timezone.now()
        locked.save(update_fields=("status", "failure_reason", "provider_response", "processed_at", "updated_at"))

    # Reconcile the reserved wallet only after a synchronous provider rejection.
    return fail_delivery_payout(
        locked,
        locked.failure_reason or "Daraja B2C rejected the payout request.",
    )


def _find_payout_from_b2c_result(result):
    originator = str(result.get("OriginatorConversationID") or "").strip()
    conversation = str(result.get("ConversationID") or "").strip()
    transaction_id = str(result.get("TransactionID") or "").strip()
    candidates = [value for value in (originator, conversation, transaction_id) if value]
    for reference in candidates:
        payout = DeliveryPayout.objects.filter(provider_reference=reference).first()
        if payout:
            return payout
        payout = DeliveryPayout.objects.filter(idempotency_key=reference).first()
        if payout:
            return payout
    return None


def _b2c_result_object(payload):
    if not isinstance(payload, dict):
        return None
    result = payload.get("Result")
    return result if isinstance(result, dict) else payload


def handle_delivery_b2c_result(payload):
    result = _b2c_result_object(payload)
    if not isinstance(result, dict):
        return None, "Invalid B2C callback payload."

    payout = _find_payout_from_b2c_result(result)
    if not payout:
        return None, "Payout reference not recognised."

    result_code = str(result.get("ResultCode", "")).strip()
    transaction_id = str(result.get("TransactionID") or "").strip()
    if result_code == "0":
        paid = complete_delivery_payout(
            payout,
            provider_reference=transaction_id or payout.provider_reference,
            provider_response=payload,
        )
        return paid, "paid"

    failed = fail_delivery_payout(
        payout,
        str(result.get("ResultDesc") or "Daraja reported that the rider payout failed."),
    )
    return failed, "failed"


def handle_delivery_b2c_timeout(payload):
    result = _b2c_result_object(payload)
    if not isinstance(result, dict):
        return None, "Invalid B2C timeout payload."

    payout = _find_payout_from_b2c_result(result)
    if not payout:
        return None, "Payout reference not recognised."

    failed = fail_delivery_payout(
        payout,
        str(
            result.get("ResultDesc")
            or result.get("ResponseDescription")
            or "Daraja reported that the rider payout request timed out."
        ),
    )
    return failed, "failed"


def complete_delivery_payout(payout, provider_reference="", provider_response=None):
    with transaction.atomic():
        locked = DeliveryPayout.objects.select_for_update().select_related("agent").get(pk=payout.pk)
        if locked.status == DeliveryPayout.STATUS_PAID:
            return locked
        if locked.status not in (DeliveryPayout.STATUS_QUEUED, DeliveryPayout.STATUS_PROCESSING):
            return locked

        wallet = get_or_create_delivery_wallet(locked.agent)
        locked.status = DeliveryPayout.STATUS_PAID
        locked.provider_reference = str(provider_reference or "")
        locked.provider_response = provider_response or {}
        locked.paid_at = timezone.now()
        locked.processed_at = timezone.now()
        locked.failure_reason = ""
        locked.save(update_fields=("status", "provider_reference", "provider_response", "paid_at", "processed_at", "failure_reason", "updated_at"))

        amount = _money(locked.amount)
        wallet.pending_payout_balance = _money(max(ZERO, wallet.pending_payout_balance - amount))
        wallet.total_paid = _money(wallet.total_paid + amount)
        wallet.last_payout_at = locked.paid_at
        wallet.save(update_fields=("pending_payout_balance", "total_paid", "last_payout_at", "updated_at"))

        DeliveryEarning.objects.filter(payout=locked, status=DeliveryEarning.STATUS_RESERVED).update(status=DeliveryEarning.STATUS_PAID)
        return locked


def fail_delivery_payout(payout, reason):
    with transaction.atomic():
        locked = DeliveryPayout.objects.select_for_update().select_related("agent").get(pk=payout.pk)
        if locked.status in (DeliveryPayout.STATUS_PAID, DeliveryPayout.STATUS_CANCELLED):
            return locked

        wallet = get_or_create_delivery_wallet(locked.agent)
        amount = _money(locked.amount)
        locked.status = DeliveryPayout.STATUS_FAILED
        locked.failure_reason = str(reason or "Payout failed.")[:255]
        locked.processed_at = timezone.now()
        locked.save(update_fields=("status", "failure_reason", "processed_at", "updated_at"))

        wallet.pending_payout_balance = _money(max(ZERO, wallet.pending_payout_balance - amount))
        wallet.available_balance = _money(wallet.available_balance + amount)
        wallet.save(update_fields=("pending_payout_balance", "available_balance", "updated_at"))
        DeliveryEarning.objects.filter(payout=locked, status=DeliveryEarning.STATUS_RESERVED).update(
            status=DeliveryEarning.STATUS_AVAILABLE,
            payout=None,
        )
        return locked


def cancel_delivery_payout(payout, reason="Cancelled by Shopiva admin."):
    with transaction.atomic():
        locked = DeliveryPayout.objects.select_for_update().select_related("agent").get(pk=payout.pk)
        if locked.status in (DeliveryPayout.STATUS_PAID, DeliveryPayout.STATUS_CANCELLED):
            return locked

        wallet = get_or_create_delivery_wallet(locked.agent)
        amount = _money(locked.amount)
        locked.status = DeliveryPayout.STATUS_CANCELLED
        locked.failure_reason = str(reason or "Payout cancelled.")[:255]
        locked.processed_at = timezone.now()
        locked.save(update_fields=("status", "failure_reason", "processed_at", "updated_at"))

        wallet.pending_payout_balance = _money(max(ZERO, wallet.pending_payout_balance - amount))
        wallet.available_balance = _money(wallet.available_balance + amount)
        wallet.save(update_fields=("pending_payout_balance", "available_balance", "updated_at"))
        DeliveryEarning.objects.filter(payout=locked, status=DeliveryEarning.STATUS_RESERVED).update(
            status=DeliveryEarning.STATUS_AVAILABLE,
            payout=None,
        )
        return locked
