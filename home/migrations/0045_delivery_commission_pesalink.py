# Generated for the commission-only delivery payout redesign.

from decimal import Decimal

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0044_seller_business_nature"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="deliverypayprofile",
            name="deliverypay_base_gte_0",
        ),
        migrations.RemoveConstraint(
            model_name="deliverypayprofile",
            name="deliverypay_km_gte_0",
        ),
        migrations.RemoveField(
            model_name="deliverypayprofile",
            name="base_per_delivery",
        ),
        migrations.RemoveField(
            model_name="deliverypayprofile",
            name="per_km_rate",
        ),
        migrations.AddField(
            model_name="deliverypayprofile",
            name="commission_percent",
            field=models.DecimalField(default=Decimal("0.00"), decimal_places=2, max_digits=5),
        ),
        migrations.AddConstraint(
            model_name="deliverypayprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(commission_percent__gte=0, commission_percent__lte=100),
                name="deliverypay_commission_0_100",
            ),
        ),
        migrations.AlterField(
            model_name="deliverypayprofile",
            name="minimum_payout",
            field=models.DecimalField(default=Decimal("100.00"), decimal_places=2, max_digits=10),
        ),
        migrations.AlterField(
            model_name="deliverypayprofile",
            name="auto_payout_threshold",
            field=models.DecimalField(default=Decimal("100.00"), decimal_places=2, max_digits=10),
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
        migrations.AlterField(
            model_name="deliverywallet",
            name="auto_payout_threshold",
            field=models.DecimalField(default=Decimal("100.00"), decimal_places=2, max_digits=10),
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
    ]
