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
        auto_payout_enabled=False,
        auto_payout_threshold=DEFAULT_MINIMUM_PAYOUT,
        is_active=True,
        notes="Commission-only rider pay. Configure the approved percentage of the customer delivery fee before enabling automatic cashout.",
    )


def get_or_create_delivery_wallet(agent):
    wallet, _ = DeliveryWallet.objects.get_or_create(
        agent=agent,
        defaults={
            "payout_phone": agent.phone or "",
            "auto_payout_enabled": False,
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


def queue_delivery_payout(agent):
    """Reserve the rider's available commission for an admin-confirmed M-Pesa payout.

    No external money movement is performed here. The rider request is placed
    in the admin approval queue. An administrator makes the M-Pesa payment
    outside the app and then records the M-Pesa transaction reference.
    """
    with transaction.atomic():
        wallet = get_or_create_delivery_wallet(agent)
        profile = get_active_delivery_pay_profile()
        minimum = _money(profile.minimum_payout)

        if DeliveryPayout.objects.filter(
            agent=agent, status__in=("requested", "processing")
        ).exists():
            return None

        available = _money(wallet.available_balance)
        if available <= ZERO:
            return None
        if available < minimum:
            raise ValueError(f"Minimum payout is KSh {minimum:,.2f}.")

        phone = (wallet.payout_phone or agent.phone or "").strip()
        if not phone:
            raise ValueError("Add a valid M-Pesa payout phone number before requesting payout.")
        phone = normalize_payout_phone(phone)
        wallet.payout_phone = phone
        wallet.save(update_fields=("payout_phone", "updated_at"))

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
            provider="admin_mpesa",
            status=DeliveryPayout.STATUS_REQUESTED,
            trigger=DeliveryPayout.TRIGGER_MANUAL,
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
    """Create rider commission only after customer receipt and payment are confirmed."""
    if not agent or not order:
        return None
    if order.status != "delivered" or not order.customer_delivery_confirmed:
        return None
    if order.payment_status != "paid":
        return None

    with transaction.atomic():
        existing = DeliveryEarning.objects.select_for_update().filter(order=order).first()
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
            distance_amount=Decimal("0.00"),
            commission_percent=commission_percent,
            commission_amount=total_amount,
            total_amount=total_amount,
            status=DeliveryEarning.STATUS_AVAILABLE,
            earned_at=now or timezone.now(),
        )
        wallet.available_balance = _money(wallet.available_balance + total_amount)
        wallet.total_earned = _money(wallet.total_earned + total_amount)
        wallet.save(update_fields=("available_balance", "total_earned", "updated_at"))
        return earning

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
