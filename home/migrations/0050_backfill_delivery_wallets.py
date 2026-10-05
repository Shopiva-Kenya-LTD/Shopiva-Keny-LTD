from decimal import Decimal

from django.db import migrations


def backfill_delivery_wallets(apps, schema_editor):
    DeliveryAgent = apps.get_model("home", "DeliveryAgent")
    DeliveryWallet = apps.get_model("home", "DeliveryWallet")
    for agent in DeliveryAgent.objects.all().iterator():
        DeliveryWallet.objects.get_or_create(
            agent_id=agent.pk,
            defaults={
                "payout_phone": agent.phone or "",
                "auto_payout_enabled": False,
                "auto_payout_threshold": Decimal("100.00"),
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0049_hash_nia_caller_pins"),
    ]

    operations = [
        migrations.RunPython(backfill_delivery_wallets, migrations.RunPython.noop),
    ]
