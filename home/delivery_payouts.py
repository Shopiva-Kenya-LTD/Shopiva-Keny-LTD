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
DEFAULT_COMMISSION_PERCENT = Decimal("0.00")
DEFAULT_MINIMUM_PAYOUT = Decimal("100.00")


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
        commission_percent=DEFAULT_COMMISSION_PERCENT,
        minimum_payout=DEFAULT_MINIMUM_PAYOUT,
        auto_payout_enabled=True,
        auto_payout_threshold=DEFAULT_MINIMUM_PAYOUT,
        is_active=True,
        notes="Commission-only rider pay. Configure the approved percentage of the customer delivery fee before enabling automatic cashout.",
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
    """Rider earns a commission on the customer delivery fee, not a base or per-km payment."""
    delivery_fee = max(Decimal("0.00"), _money(order.delivery_fee))
    commission_percent = max(Decimal("0.00"), min(Decimal("100.00"), _money(profile.commission_percent)))
    total_amount = _money(delivery_fee * commission_percent / Decimal("100.00"))
    return delivery_fee, commission_percent, total_amount


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

        bank_name = (wallet.bank_name or "").strip()
        bank_code = (wallet.bank_code or "").strip()
        bank_account_name = (wallet.bank_account_name or agent.display_name or "").strip()
        bank_account_number = (wallet.bank_account_number or "").strip()
        if not bank_name or not bank_code or not bank_account_name or not bank_account_number:
            raise ValueError("Add the rider's Kenyan bank name, bank code, account name and account number before requesting payout.")

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
            phone="",
            bank_name=bank_name,
            bank_code=bank_code,
            bank_account_name=bank_account_name,
            bank_account_number=bank_account_number,
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
        delivery_fee, commission_percent, total_amount = calculate_delivery_earning(order, profile)
        if total_amount <= ZERO:
            raise ValueError("Rider commission is not configured or the customer delivery fee is zero.")
        wallet = get_or_create_delivery_wallet(agent)

        earning = DeliveryEarning.objects.create(
            agent=agent,
            order=order,
            distance_km=max(Decimal("0.00"), _money(order.delivery_distance_km)),
            distance_source=order.delivery_distance_source or "estimated",
            pay_profile=profile,
            base_amount=delivery_fee,
            distance_amount=commission_percent,
            total_amount=total_amount,
            status=DeliveryEarning.STATUS_AVAILABLE,
            earned_at=now or timezone.now(),
        )
        wallet.available_balance = _money(wallet.available_balance + total_amount)
        wallet.total_earned = _money(wallet.total_earned + total_amount)
        wallet.save(update_fields=("available_balance", "total_earned", "updated_at"))

        return earning


def pesalink_ready():
    """Return True when Shopiva can automatically disburse rider earnings through PesaLink."""
    if os.getenv("PESALINK_ENABLED", "false").strip().lower() != "true":
        return False
    required = (
        "INTASEND_API_TOKEN",
        "INTASEND_DEVICE_ID",
        "INTASEND_PAYOUT_WALLET_ID",
        "INTASEND_PAYOUT_CALLBACK_URL",
    )
    return all(os.getenv(name, "").strip() for name in required)


def _intasend_request(path, payload):
    token = os.getenv("INTASEND_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("IntaSend payout API token is not configured.")
    request = urllib.request.Request(
        os.getenv("INTASEND_API_BASE_URL", "https://api.intasend.com").rstrip("/") + path,
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
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw}


def initiate_delivery_payout(payout):
    """Submit a queued rider commission payout through IntaSend PesaLink.

    The customer confirmation has already released the earning into the rider
    wallet. The external provider callback is the only event allowed to mark
    the payout paid.
    """
    if not payout or not pesalink_ready():
        return payout

    with transaction.atomic():
        locked = DeliveryPayout.objects.select_for_update().get(pk=payout.pk)
        if locked.status == DeliveryPayout.STATUS_PAID:
            return locked
        if locked.status not in (DeliveryPayout.STATUS_QUEUED, DeliveryPayout.STATUS_PROCESSING):
            return locked
        state = dict(locked.provider_response or {})
        state["provider"] = "pesalink"
        locked.status = DeliveryPayout.STATUS_PROCESSING
        locked.provider = "pesalink"
        locked.provider_response = state
        locked.failure_reason = ""
        locked.save(update_fields=("status", "provider", "provider_response", "failure_reason", "updated_at"))

    payload = {
        "currency": "KES",
        "provider": "PESALINK",
        "device_id": os.getenv("INTASEND_DEVICE_ID", "").strip(),
        "callback_url": os.getenv("INTASEND_PAYOUT_CALLBACK_URL", "").strip(),
        "batch_reference": payout.idempotency_key,
        "requires_approval": "NO",
        "transactions": [{
            "name": payout.bank_account_name,
            "account": payout.bank_account_number,
            "bank_code": payout.bank_code,
            "amount": str(_money(payout.amount)),
            "narrative": f"Shopiva rider commission #{payout.id}",
            "country": "KE",
        }],
    }

    try:
        status, response = _intasend_request("/api/v1/send-money/initiate/", payload)
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
        accepted = status in (200, 201) and not response.get("error")
        if accepted:
            locked.status = DeliveryPayout.STATUS_PROCESSING
            locked.provider_reference = str(
                response.get("tracking_id")
                or response.get("trackingId")
                or response.get("id")
                or locked.idempotency_key
            )
            locked.provider_response = state
            locked.save(update_fields=("status", "provider_reference", "provider_response", "updated_at"))
            return locked

        locked.status = DeliveryPayout.STATUS_FAILED
        locked.failure_reason = str(
            response.get("detail")
            or response.get("error")
            or response.get("message")
            or f"PesaLink payout rejected (HTTP {status})."
        )[:255]
        locked.provider_response = state
        locked.processed_at = timezone.now()
        locked.save(update_fields=("status", "failure_reason", "provider_response", "processed_at", "updated_at"))

    return fail_delivery_payout(locked, locked.failure_reason or "PesaLink rejected the payout request.")


def handle_pesalink_webhook(payload):
    if not isinstance(payload, dict):
        return None, "Invalid PesaLink payout callback payload."

    tracking_id = str(payload.get("tracking_id") or "").strip()
    transactions = payload.get("transactions") or []
    transaction = transactions[0] if transactions and isinstance(transactions[0], dict) else {}
    request_reference = str(
        transaction.get("request_reference_id")
        or transaction.get("idempotency_key")
        or ""
    ).strip()
    provider_reference = str(transaction.get("provider_reference") or transaction.get("transaction_id") or "").strip()

    payout = None
    for reference in (tracking_id, request_reference, provider_reference):
        if reference:
            payout = DeliveryPayout.objects.filter(provider_reference=reference).first()
            if payout:
                break
            payout = DeliveryPayout.objects.filter(idempotency_key=reference).first()
            if payout:
                break

    if not payout:
        return None, "Payout reference not recognised."

    status = str(transaction.get("status") or payload.get("status") or "").strip().casefold()
    code = str(transaction.get("status_code") or "").strip().upper()

    if status in {"successful", "success", "complete", "completed"} or code == "TS100":
        paid = complete_delivery_payout(
            payout,
            provider_reference=provider_reference or tracking_id,
            provider_response=payload,
        )
        return paid, "paid"

    if status in {"failed", "unsuccessful", "cancelled", "canceled"} or code in {"TF106", "TC108", "TF103"}:
        failed = fail_delivery_payout(
            payout,
            str(transaction.get("status_description") or "PesaLink reported that the payout failed."),
        )
        return failed, "failed"

    return payout, "processing"


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
