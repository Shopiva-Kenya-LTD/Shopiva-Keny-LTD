from django.db import migrations, models


def migrate_legacy_rider_payouts(apps, schema_editor):
    DeliveryPayout = apps.get_model("home", "DeliveryPayout")
    DeliveryPayout.objects.filter(status="queued").update(status="requested")
    DeliveryPayout.objects.filter(status__in=["requested", "processing"], provider="pesalink").update(provider="admin_mpesa")
    DeliveryPayout.objects.filter(status__in=["requested", "processing"], trigger="automatic").update(trigger="manual")


def reverse_legacy_rider_payouts(apps, schema_editor):
    DeliveryPayout = apps.get_model("home", "DeliveryPayout")
    DeliveryPayout.objects.filter(status="requested").update(status="queued")


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0045_delivery_commission_pesalink"),
    ]

    operations = [
        migrations.RunPython(migrate_legacy_rider_payouts, reverse_legacy_rider_payouts),
        migrations.AlterField(
            model_name="deliverypayout",
            name="status",
            field=models.CharField(
                choices=[
                    ("requested", "Awaiting admin payment"),
                    ("processing", "Processing"),
                    ("paid", "Paid"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="requested",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="deliverypayout",
            name="trigger",
            field=models.CharField(
                choices=[("manual", "Rider requested"), ("admin", "Admin")],
                default="manual",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="deliverypayout",
            name="provider",
            field=models.CharField(default="admin_mpesa", max_length=30),
        ),
        migrations.AlterField(
            model_name="deliverypayprofile",
            name="auto_payout_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="deliverywallet",
            name="auto_payout_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
