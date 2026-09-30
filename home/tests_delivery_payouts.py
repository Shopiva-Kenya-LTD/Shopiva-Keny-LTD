from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import DeliveryAgent, DeliveryEarning, DeliveryPayProfile, DeliveryPayout, DeliveryWallet, Order
from .delivery_payouts import complete_delivery_payout, fail_delivery_payout, queue_delivery_payout


class DeliveryPayoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rider1", password="StrongPass123!")
        self.customer = User.objects.create_user(username="customer1", password="StrongPass123!")
        self.admin = User.objects.create_superuser(username="admin1", email="admin@example.com", password="StrongPass123!")
        self.agent = DeliveryAgent.objects.create(user=self.user, phone="0712345678", is_active=True)
        self.client = Client()
        self.profile = DeliveryPayProfile.objects.get(name="Shopiva Standard Rider Pay")
        self.profile.commission_percent = Decimal("20.00")
        self.profile.minimum_payout = Decimal("100.00")
        self.profile.auto_payout_enabled = False
        self.profile.save(update_fields=("commission_percent", "minimum_payout", "auto_payout_enabled", "updated_at"))

    def make_order(self, delivery_fee="500.00", code="123456", payment_status="paid"):
        return Order.objects.create(
            customer=self.customer,
            customer_name="Customer",
            email="customer@example.com",
            phone="0722000000",
            address="Eldoret",
            total_amount=Decimal("1500.00"),
            items_subtotal=Decimal("1000.00"),
            delivery_fee=Decimal(delivery_fee),
            delivery_distance_km=Decimal("10.00"),
            delivery_distance_source="routes_api",
            delivery_agent=self.agent,
            status="out_for_delivery",
            payment_status=payment_status,
            delivery_confirmation_code=code,
        )

    def mark_delivered_as_rider(self, order, code="123456"):
        self.client.force_login(self.user)
        return self.client.post(
            f"/delivery/order/{order.id}/action/",
            {"action": "delivered", "code": code},
        )

    def confirm_receipt(self, order):
        self.client.force_login(self.customer)
        return self.client.post(
            f"/account/orders/{order.id}/",
            {"action": "confirm_received"},
        )

    def test_rider_commission_waits_for_customer_receipt(self):
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)

        self.assertFalse(DeliveryEarning.objects.filter(order=order).exists())
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(wallet.total_earned, Decimal("0.00"))

        self.confirm_receipt(order)
        earning = DeliveryEarning.objects.get(order=order)
        self.assertEqual(earning.commission_percent, Decimal("20.00"))
        self.assertEqual(earning.commission_amount, Decimal("100.00"))
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))

    def test_customer_confirmation_requires_payment(self):
        order = self.make_order("500.00", payment_status="pending")
        self.mark_delivered_as_rider(order)
        self.confirm_receipt(order)
        self.assertFalse(DeliveryEarning.objects.filter(order=order).exists())
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_rider_request_creates_admin_review_payout(self):
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)
        self.confirm_receipt(order)

        payout = queue_delivery_payout(self.agent)
        self.assertEqual(payout.status, DeliveryPayout.STATUS_REQUESTED)
        self.assertEqual(payout.provider, "admin_mpesa")
        self.assertEqual(payout.trigger, DeliveryPayout.TRIGGER_MANUAL)
        self.assertEqual(payout.phone, "254712345678")
        self.assertEqual(payout.amount, Decimal("100.00"))

        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(wallet.pending_payout_balance, Decimal("100.00"))
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_admin_marks_manual_mpesa_payout_paid(self):
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)
        self.confirm_receipt(order)
        payout = queue_delivery_payout(self.agent)

        complete_delivery_payout(payout, provider_reference="SG123ABC")
        payout.refresh_from_db()
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(payout.status, DeliveryPayout.STATUS_PAID)
        self.assertEqual(payout.provider_reference, "SG123ABC")
        self.assertEqual(wallet.pending_payout_balance, Decimal("0.00"))
        self.assertEqual(wallet.total_paid, Decimal("100.00"))

    def test_failed_admin_payout_returns_money_to_available_balance(self):
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)
        self.confirm_receipt(order)
        payout = queue_delivery_payout(self.agent)

        fail_delivery_payout(payout, "Admin could not complete the M-Pesa transfer.")
        payout.refresh_from_db()
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(payout.status, DeliveryPayout.STATUS_FAILED)
        self.assertEqual(wallet.available_balance, Decimal("100.00"))
        self.assertEqual(wallet.pending_payout_balance, Decimal("0.00"))
