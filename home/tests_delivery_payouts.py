from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import DeliveryAgent, DeliveryEarning, DeliveryPayProfile, DeliveryPayout, DeliveryWallet, Order
from .delivery_payouts import complete_delivery_payout, fail_delivery_payout, queue_delivery_payout


class DeliveryPayoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rider1", password="StrongPass123!")
        self.customer = User.objects.create_user(username="customer1", password="StrongPass123!")
        self.agent = DeliveryAgent.objects.create(user=self.user, phone="0712345678", is_active=True)
        self.client = Client()
        self.profile = DeliveryPayProfile.objects.get(name="Shopiva Standard Rider Pay")
        self.profile.commission_percent = Decimal("20.00")
        self.profile.minimum_payout = Decimal("100.00")
        self.profile.auto_payout_threshold = Decimal("100.00")
        self.profile.auto_payout_enabled = True
        self.profile.save(update_fields=("commission_percent", "minimum_payout", "auto_payout_threshold", "auto_payout_enabled", "updated_at"))

    def make_order(self, delivery_fee="500.00", code="123456"):
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
            delivery_confirmation_code=code,
        )

    def mark_delivered_as_rider(self, order, code="123456"):
        self.client.force_login(self.user)
        return self.client.post(
            f"/delivery/order/{order.id}/action/",
            {"action": "delivered", "code": code},
        )

    def test_rider_commission_is_percentage_of_delivery_fee(self):
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)

        self.assertFalse(DeliveryEarning.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")
        self.assertFalse(order.customer_delivery_confirmed)

        self.client.force_login(self.customer)
        response = self.client.post(
            f"/account/orders/{order.id}/",
            {"action": "confirm_received"},
        )
        self.assertEqual(response.status_code, 302)

        earning = DeliveryEarning.objects.get(order=order)
        self.assertEqual(earning.base_amount, Decimal("500.00"))
        self.assertEqual(earning.commission_percent, Decimal("20.00"))
        self.assertEqual(earning.commission_amount, Decimal("100.00"))
        self.assertEqual(earning.total_amount, Decimal("100.00"))

    def test_customer_confirmation_is_required_before_wallet_credit(self):
        order = self.make_order("1000.00")
        self.mark_delivered_as_rider(order)

        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(wallet.total_earned, Decimal("0.00"))
        self.assertFalse(order.customer_delivery_confirmed)

        self.client.force_login(self.customer)
        self.client.post(
            f"/account/orders/{order.id}/",
            {"action": "confirm_received"},
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.total_earned, Decimal("200.00"))
        order.refresh_from_db()
        self.assertTrue(order.customer_delivery_confirmed)
        self.assertEqual(order.customer_delivery_confirmed_by_id, self.customer.id)

    def test_automatic_payout_queues_after_customer_confirmation(self):
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        wallet.bank_name = "Co-operative Bank"
        wallet.bank_code = "11"
        wallet.bank_account_name = "Rider One"
        wallet.bank_account_number = "0123456789"
        wallet.save(update_fields=("bank_name", "bank_code", "bank_account_name", "bank_account_number", "updated_at"))
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)
        self.client.force_login(self.customer)
        self.client.post(
            f"/account/orders/{order.id}/",
            {"action": "confirm_received"},
        )

        payout = DeliveryPayout.objects.get(agent=self.agent)
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        self.assertEqual(payout.status, DeliveryPayout.STATUS_QUEUED)
        self.assertEqual(payout.provider, "pesalink")
        self.assertEqual(payout.amount, Decimal("100.00"))
        self.assertEqual(wallet.pending_payout_balance, Decimal("100.00"))

    def test_manual_payout_requires_bank_details(self):
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)
        self.client.force_login(self.customer)
        self.client.post(
            f"/account/orders/{order.id}/",
            {"action": "confirm_received"},
        )

        wallet = DeliveryWallet.objects.get(agent=self.agent)
        wallet.bank_name = "Co-operative Bank"
        wallet.bank_code = "11"
        wallet.bank_account_name = "Rider One"
        wallet.bank_account_number = "0123456789"
        wallet.save(update_fields=("bank_name", "bank_code", "bank_account_name", "bank_account_number", "updated_at"))

        payout = queue_delivery_payout(self.agent, automatic=False, force=True)
        self.assertEqual(payout.provider, "pesalink")
        self.assertEqual(payout.bank_name, "Co-operative Bank")
        self.assertEqual(payout.bank_account_number, "0123456789")

    def test_payout_success_and_failure_reconcile_wallet(self):
        order = self.make_order("500.00")
        self.mark_delivered_as_rider(order)
        self.client.force_login(self.customer)
        self.client.post(
            f"/account/orders/{order.id}/",
            {"action": "confirm_received"},
        )
        wallet = DeliveryWallet.objects.get(agent=self.agent)
        wallet.bank_name = "Co-operative Bank"
        wallet.bank_code = "11"
        wallet.bank_account_name = "Rider One"
        wallet.bank_account_number = "0123456789"
        wallet.save(update_fields=("bank_name", "bank_code", "bank_account_name", "bank_account_number", "updated_at"))

        payout = DeliveryPayout.objects.get(agent=self.agent)
        complete_delivery_payout(payout, provider_reference="PSL-123")
        wallet.refresh_from_db()
        self.assertEqual(payout.refresh_from_db(), None)
        payout.refresh_from_db()
        self.assertEqual(payout.status, DeliveryPayout.STATUS_PAID)
        self.assertEqual(wallet.pending_payout_balance, Decimal("0.00"))
        self.assertEqual(wallet.total_paid, Decimal("100.00"))

        second = self.make_order("500.00", "654321")
        self.mark_delivered_as_rider(second, "654321")
        self.client.force_login(self.customer)
        self.client.post(
            f"/account/orders/{second.id}/",
            {"action": "confirm_received"},
        )
        payout2 = DeliveryPayout.objects.filter(agent=self.agent, status=DeliveryPayout.STATUS_QUEUED).exclude(pk=payout.pk).first()
        if payout2:
            fail_delivery_payout(payout2, "Provider rejected test payout.")
            wallet.refresh_from_db()
            self.assertEqual(wallet.available_balance, Decimal("100.00"))
