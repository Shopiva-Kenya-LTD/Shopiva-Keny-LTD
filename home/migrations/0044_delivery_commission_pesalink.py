from django.db import migrations, models
from django.conf import settings
from decimal import Decimal


def migrate_legacy_profile(apps, schema_editor):
    Profile = apps.get_model("home", "DeliveryPayProfile")
    for profile in Profile.objects.all():
        profile.commission_percent = Decimal("0.00")
        profile.minimum_payout = Decimal("100.00")
        profile.auto_payout_threshold = Decimal("100.00")
        profile.notes = (
            "Rider earnings are commission-only: a percentage of the customer delivery fee "
            "after customer delivery confirmation. Distance determines the customer delivery "
            "fee, not a separate rider per-kilometre payment."
        )
        profile.save(update_fields=("commission_percent", "minimum_payout", "auto_payout_threshold", "notes", "updated_at"))
    Wallet = apps.get_model("home", "DeliveryWallet")
    Wallet.objects.all().update(auto_payout_threshold=Decimal("100.00"))


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0043_delivery_staff_payouts"),
    ]

    operations = [
        migrations.AddField(
            model_name="deliverypayprofile",
            name="commission_percent",
            field=models.DecimalField(default=Decimal("0.00"), decimal_places=2, max_digits=5),
        ),
        migrations.AddField(
            model_name="deliverywallet",
            name="bank_name",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="deliverywallet",
            name="bank_code",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="deliverywallet",
            name="bank_account_name",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="deliverywallet",
            name="bank_account_number",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="order",
            name="customer_delivery_confirmed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="order",
            name="customer_delivery_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="customer_delivery_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="confirmed_shopiva_deliveries",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="deliverypayprofile",
            name="minimum_payout",
            field=models.DecimalField(default=Decimal("100.00"), decimal_places=2, max_digits=10),
        ),
        migrations.AddField(
            model_name="deliveryearning",
            name="commission_percent",
            field=models.DecimalField(default=Decimal("0.00"), decimal_places=2, max_digits=5),
        ),
        migrations.AddField(
            model_name="deliveryearning",
            name="commission_amount",
            field=models.DecimalField(default=Decimal("0.00"), decimal_places=2, max_digits=10),
        ),
        migrations.AlterField(
            model_name="deliveryearning",
            name="base_amount",
            field=models.DecimalField(default=Decimal("0.00"), decimal_places=2, max_digits=10),
        ),
        migrations.AlterField(
            model_name="deliveryearning",
            name="distance_amount",
            field=models.DecimalField(default=Decimal("0.00"), decimal_places=2, max_digits=10),
        ),
        migrations.AddField(
            model_name="deliverypayout",
            name="bank_name",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="deliverypayout",
            name="bank_code",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="deliverypayout",
            name="bank_account_name",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="deliverypayout",
            name="bank_account_number",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AlterField(
            model_name="deliverypayout",
            name="phone",
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AlterField(
            model_name="deliverypayout",
            name="provider",
            field=models.CharField(default="pesalink", max_length=30),
        ),
        migrations.RunPython(migrate_legacy_profile, migrations.RunPython.noop),
    ]
