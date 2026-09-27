from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import (
    DeliveryAgent,
    DeliveryEarning,
    DeliveryPayProfile,
    DeliveryPayout,
    DeliveryWallet,
    Order,
)
from .delivery_payouts import (
    complete_delivery_payout,
    fail_delivery_payout,
    queue_delivery_payout,
)


class DeliveryPayoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rider1",
            password="StrongPass123!",
        )
        self.agent = DeliveryAgent.objects.create(
            user=self.user,
            phone="0712345678",
            is_active=True,
        )
        self.client = Client()
        self.profile = DeliveryPayProfile.objects.get(name="Shopiva Standard Rider Pay")
        self.profile.auto_payout_threshold = Decimal("500.00")
        self.profile.save(update_fields=("auto_payout_threshold", "updated_at"))

    def make_order(self, distance, code):
        return Order.objects.create(
            customer_name="Customer",
            email="customer@example.com",
            phone="0722000000",
            address="Eldoret",
            total_amount=Decimal("1000.00"),
            items_subtotal=Decimal("1000.00"),
            delivery_distance_km=Decimal(str(distance)),
            delivery_distance_source="routes_api",
            delivery_agent=self.agent,
            status="out_for_delivery",
            delivery_confirmation_code=code,
        )

    def test_base_plus_distance_earning(self):
        order = self.make_order("10.00", "123456")
        self.client.force_login(self.user)

        response = self.client.post(
            f"/delivery/order/{order.id}/action/",
            {"action": "delivered", "code": "123456"},
        )

        self.assertEqual(response.status_code, 200)
        earning = DeliveryEarning.objects.get(order=order)
        self.assertEqual(earning.base_amount, Decimal("100.00"))
        self.assertEqual(earning.distance_amount, Decimal("150.00"))
        self.assertEqual(earning.total_amount, Decimal("250.00"))
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(wallet.available_balance, Decimal("250.00"))
        self.assertEqual(wallet.total_earned, Decimal("250.00"))

    def test_automatic_payout_queues_at_threshold(self):
        first = self.make_order("10.00", "123456")
        second = self.make_order("20.00", "234567")
        self.client.force_login(self.user)

        self.client.post(
            f"/delivery/order/{first.id}/action/",
            {"action": "delivered", "code": "123456"},
        )
        self.client.post(
            f"/delivery/order/{second.id}/action/",
            {"action": "delivered", "code": "234567"},
        )

        payout = DeliveryPayout.objects.get(agent=self.agent)
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(payout.status, DeliveryPayout.STATUS_QUEUED)
        self.assertEqual(payout.trigger, DeliveryPayout.TRIGGER_AUTOMATIC)
        self.assertEqual(payout.amount, Decimal("650.00"))
        self.assertEqual(wallet.available_balance, Decimal("0.00"))
        self.assertEqual(wallet.pending_payout_balance, Decimal("650.00"))
        self.assertEqual(
            DeliveryEarning.objects.filter(payout=payout, status=DeliveryEarning.STATUS_RESERVED).count(),
            2,
        )

    def test_manual_payout_requires_and_uses_wallet_phone(self):
        self.profile.auto_payout_enabled = False
        self.profile.save(update_fields=("auto_payout_enabled", "updated_at"))
        wallet = DeliveryWallet.objects.create(
            agent=self.agent,
            payout_phone=self.agent.phone,
            auto_payout_enabled=False,
            auto_payout_threshold=Decimal("9999.00"),
        )
        order = self.make_order("40.00", "123456")
        self.client.force_login(self.user)
        self.client.post(
            f"/delivery/order/{order.id}/action/",
            {"action": "delivered", "code": "123456"},
        )

        wallet = DeliveryWallet.objects.get(agent=self.agent)
        wallet.auto_payout_threshold = Decimal("9999.00")
        wallet.payout_phone = "0712345678"
        wallet.save(update_fields=("auto_payout_threshold", "payout_phone", "updated_at"))

        self.profile.minimum_payout = Decimal("1000.00")
        self.profile.save(update_fields=("minimum_payout", "updated_at"))
        with self.assertRaises(ValueError):
            queue_delivery_payout(self.agent, automatic=False)

        self.profile.minimum_payout = Decimal("500.00")
        self.profile.save(update_fields=("minimum_payout", "updated_at"))
        payout = queue_delivery_payout(self.agent, automatic=False)
        self.assertIsNotNone(payout)
        self.assertEqual(payout.phone, "254712345678")
        self.assertEqual(payout.amount, Decimal("700.00"))

    def test_payout_success_and_failure_reconcile_wallet(self):
        order = self.make_order("10.00", "123456")
        self.client.force_login(self.user)
        self.client.post(
            f"/delivery/order/{order.id}/action/",
            {"action": "delivered", "code": "123456"},
        )

        wallet = DeliveryWallet.objects.get(agent=self.agent)
        wallet.payout_phone = "0712345678"
        wallet.auto_payout_threshold = Decimal("9999.00")
        wallet.save(update_fields=("payout_phone", "auto_payout_threshold", "updated_at"))

        payout = queue_delivery_payout(self.agent, automatic=False, force=True)
        self.assertEqual(wallet.refresh_from_db(), None)
        self.assertEqual(DeliveryWallet.objects.get(agent=self.agent).pending_payout_balance, Decimal("250.00"))

        complete_delivery_payout(payout, provider_reference="B2C-123")
        wallet.refresh_from_db()
        payout.refresh_from_db()
        earning = DeliveryEarning.objects.get(order=order)
        self.assertEqual(payout.status, DeliveryPayout.STATUS_PAID)
        self.assertEqual(wallet.pending_payout_balance, Decimal("0.00"))
        self.assertEqual(wallet.total_paid, Decimal("250.00"))
        self.assertEqual(earning.status, DeliveryEarning.STATUS_PAID)

        second = self.make_order("10.00", "654321")
        self.client.post(
            f"/delivery/order/{second.id}/action/",
            {"action": "delivered", "code": "654321"},
        )
        payout2 = queue_delivery_payout(self.agent, automatic=False, force=True)
        fail_delivery_payout(payout2, "Provider rejected test payout.")
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("250.00"))
