"""Signals that keep delivery payout accounts in sync with rider accounts."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import DeliveryAgent, DeliveryWallet


@receiver(post_save, sender=DeliveryAgent)
def ensure_delivery_wallet(sender, instance, created, **kwargs):
    """Create the rider wallet as part of rider provisioning.

    The wallet is a ledger container only; it does not move money or trigger
    an external payout. Actual payout remains subject to the delivery
    completion/customer receipt rules and the admin payment workflow.
    """
    if created:
        DeliveryWallet.objects.get_or_create(
            agent=instance,
            defaults={
                "payout_phone": instance.phone or "",
                "auto_payout_enabled": False,
                "auto_payout_threshold": 100,
            },
        )
