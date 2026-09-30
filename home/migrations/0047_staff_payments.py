from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0046_admin_mpesa_delivery_payouts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("payment_method", models.CharField(choices=[("mpesa", "M-PESA"), ("bank", "Bank transfer"), ("cash", "Cash"), ("other", "Other")], default="mpesa", max_length=20)),
                ("destination", models.CharField(blank=True, help_text="M-PESA number, bank account/reference, or other payment destination.", max_length=120)),
                ("purpose", models.CharField(max_length=160)),
                ("notes", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("pending", "Pending payment"), ("processing", "Processing"), ("paid", "Paid"), ("cancelled", "Cancelled")], default="pending", max_length=20)),
                ("provider_reference", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("idempotency_key", models.CharField(default=uuid.uuid4, max_length=120, unique=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.SET_NULL, related_name="staff_payments_created", to=settings.AUTH_USER_MODEL, null=True)),
                ("paid_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="staff_payments_paid", to=settings.AUTH_USER_MODEL)),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="staff_payments_received", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(
            model_name="staffpayment",
            constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="staffpayment_amount_gt_0"),
        ),
    ]
