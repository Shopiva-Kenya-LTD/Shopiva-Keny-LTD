import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.http import HttpResponseRedirect
from django.db import IntegrityError
from django.test import TestCase
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.template.loader import get_template
from unittest.mock import patch

from .commission import get_platform_commission_percent, split_sale_amount
from .forms import CustomerRegistrationForm, SellerRegistrationForm, DeliveryRegistrationForm
from .models import (
    CustomerAddress,
    DeliveryAgent,
    DeliveryLocationPing,
    Order,
    OrderItem,
    PaymentTransaction,
    SellerProfile,
    SellerSettlement,
    SellerWallet,
    Product,
    ProductReview,
    SellerPayoutRequest,
)
from .payments import _create_seller_settlements


class ReleaseSecurityHardeningTests(TestCase):
    def test_admin_logout_requires_post_and_csrf(self):
        admin = User.objects.create_user("release_admin", password="StrongPass123!", is_staff=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(admin)
        self.assertEqual(client.get(reverse("admin_logout")).status_code, 405)
        self.assertEqual(client.post(reverse("admin_logout")).status_code, 403)

    def test_seller_payout_reserves_balance_and_blocks_duplicate_open_requests(self):
        user = User.objects.create_user("payout_seller", password="StrongPass123!")
        seller = SellerProfile.objects.create(user=user, business_name="Payout Seller")
        wallet = SellerWallet.objects.create(seller=seller, available_balance=Decimal("500.00"))
        self.client.force_login(user)

        response = self.client.post(
            reverse("seller_request_payout"), {"amount": "300.00", "phone": "0712345678"}
        )
        self.assertEqual(response.status_code, 302)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("200.00"))
        self.assertEqual(SellerPayoutRequest.objects.filter(seller=seller).count(), 1)

        self.client.post(reverse("seller_request_payout"), {"amount": "100.00", "phone": "0712345678"})
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("200.00"))
        self.assertEqual(SellerPayoutRequest.objects.filter(seller=seller).count(), 1)

    def test_product_creation_template_resolves_to_current_home_template(self):
        self.assertIn(
            "/home/templates/admin/products/form.html",
            str(get_template("admin/products/form.html").origin.name),
        )


class CustomerRegistrationTests(TestCase):
    def test_duplicate_username_is_rejected_case_insensitively(self):
        User.objects.create_user(username="McBrianTech", email="one@example.com", password="StrongPass123!")
        form = CustomerRegistrationForm(data={
            "username": "mCbRiAnTeCh", "email": "two@example.com",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("Username exists", str(form.errors["username"]))

    def test_duplicate_email_is_rejected_case_insensitively(self):
        User.objects.create_user(username="firstuser", email="User@Example.com", password="StrongPass123!")
        form = CustomerRegistrationForm(data={
            "username": "seconduser", "email": "user@example.com",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("already registered", str(form.errors["email"]))


class SellerRegistrationTests(TestCase):
    def test_duplicate_username_is_rejected_case_insensitively(self):
        User.objects.create_user(username="SellerPrime", email="seller1@example.com", password="StrongPass123!")
        form = SellerRegistrationForm(data={
            "username": "sellerprime", "email": "seller2@example.com",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("Username exists", str(form.errors["username"]))



class AccountBoundaryTests(TestCase):
    def setUp(self):
        self.password = "StrongPass123!"
        self.customer = User.objects.create_user(
            username="customer_boundary",
            email="customer-boundary@example.com",
            password=self.password,
        )
        self.seller_user = User.objects.create_user(
            username="seller_boundary",
            email="seller-boundary@example.com",
            password=self.password,
        )
        self.seller = SellerProfile.objects.create(
            user=self.seller_user,
            business_name="Boundary Seller",
            is_active=True,
        )
        SellerWallet.objects.create(seller=self.seller)
        self.delivery_user = User.objects.create_user(
            username="delivery_boundary",
            email="delivery-boundary@example.com",
            password=self.password,
        )
        DeliveryAgent.objects.create(user=self.delivery_user, is_active=True)

    def test_seller_credentials_are_rejected_by_customer_login(self):
        response = self.client.post(
            reverse("customer_login"),
            {"username": self.seller_user.username, "password": self.password},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "seller account")
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_customer_credentials_are_rejected_by_seller_login(self):
        response = self.client.post(
            reverse("seller_login"),
            {"username": self.customer.username, "password": self.password},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "customer account")
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_delivery_credentials_are_rejected_by_customer_login(self):
        response = self.client.post(
            reverse("customer_login"),
            {"username": self.delivery_user.username, "password": self.password},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "delivery account")
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_customer_cannot_become_seller_in_same_authenticated_session(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("seller_register"), secure=True)
        self.assertRedirects(response, reverse("customer_dashboard"), fetch_redirect_response=False)

    def test_authenticated_seller_customer_login_goes_to_seller_workspace(self):
        self.client.force_login(self.seller_user)
        response = self.client.get(reverse("customer_login"), secure=True)
        self.assertRedirects(response, reverse("seller_dashboard"), fetch_redirect_response=False)

    def test_authenticated_customer_seller_login_goes_to_customer_workspace(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("seller_login"), secure=True)
        self.assertRedirects(response, reverse("customer_dashboard"), fetch_redirect_response=False)

    def test_customer_dashboard_rejects_seller_session(self):
        self.client.force_login(self.seller_user)
        response = self.client.get(reverse("customer_dashboard"), secure=True)
        self.assertRedirects(response, reverse("seller_dashboard"), fetch_redirect_response=False)

    def test_seller_dashboard_rejects_customer_session(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("seller_dashboard"), secure=True)
        self.assertRedirects(response, reverse("seller_login"), fetch_redirect_response=False)


class CommissionScheduleTests(TestCase):
    def test_commission_rate_rises_with_price(self):
        self.assertEqual(get_platform_commission_percent(Decimal("500.00")), Decimal("5.00"))
        self.assertEqual(get_platform_commission_percent(Decimal("1000.00")), Decimal("7.50"))
        self.assertEqual(get_platform_commission_percent(Decimal("5000.00")), Decimal("10.00"))
        self.assertEqual(get_platform_commission_percent(Decimal("10000.00")), Decimal("12.50"))
        self.assertEqual(get_platform_commission_percent(Decimal("50000.00")), Decimal("15.00"))

    def test_sale_split_is_price_based(self):
        rate, commission, seller_amount = split_sale_amount(Decimal("10000.00"))
        self.assertEqual(rate, Decimal("12.50"))
        self.assertEqual(commission, Decimal("1250.00"))
        self.assertEqual(seller_amount, Decimal("10000.00"))


class AdminLoginTests(TestCase):
    def test_admin_login_page_loads(self):
        response = self.client.get(reverse("admin_login"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Admin Control Center")

    def test_staff_can_login_to_control_center(self):
        User.objects.create_user(username="admin_test", email="admin@example.com", password="StrongPass123!", is_staff=True)
        response = self.client.post(
            reverse("admin_login"),
            {"username": "admin_test", "password": "StrongPass123!"},
            secure=True,
        )
        self.assertRedirects(response, reverse("shopiva_admin:index"), fetch_redirect_response=False)

    def test_admin_logout_requires_post(self):
        admin = User.objects.create_user(
            username="admin_logout_test",
            email="admin-logout@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("admin_logout"), secure=True)
        self.assertEqual(response.status_code, 405)
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_admin_logout_accepts_csrf_protected_post(self):
        admin = User.objects.create_user(
            username="admin_logout_post",
            email="admin-logout-post@example.com",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client.force_login(admin)
        response = self.client.post(reverse("admin_logout"), secure=True)
        self.assertRedirects(response, reverse("admin_login"), fetch_redirect_response=False)

    def test_customer_cannot_login_to_control_center(self):
        User.objects.create_user(username="customer_test", email="customer@example.com", password="StrongPass123!", is_staff=False)
        response = self.client.post(
            reverse("admin_login"),
            {"username": "customer_test", "password": "StrongPass123!"},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not authorized")


class SellerSettlementTests(TestCase):
    def test_confirmed_order_creates_pending_seller_settlement(self):
        user = User.objects.create_user(username="seller", email="seller@example.com", password="StrongPass123!")
        seller = SellerProfile.objects.create(user=user, business_name="Seller Shop")
        SellerWallet.objects.create(seller=seller)
        product = Product.objects.create(name="Test Phone", sku="TEST-001", price=Decimal("10000.00"), stock_quantity=5, seller=seller)
        order = Order.objects.create(customer_name="Buyer", email="buyer@example.com", phone="254700000000", address="Eldoret", total_amount=Decimal("10000.00"))
        OrderItem.objects.create(order=order, product=product, quantity=1, price=Decimal("10000.00"), seller=seller, seller_gross=Decimal("10000.00"), platform_commission=Decimal("1250.00"), seller_net=Decimal("10000.00"))

        _create_seller_settlements(order)
        settlement = SellerSettlement.objects.get(order=order, seller=seller)
        wallet = SellerWallet.objects.get(seller=seller)
        self.assertEqual(settlement.seller_amount, Decimal("10000.00"))
        self.assertEqual(settlement.platform_commission, Decimal("1250.00"))
        self.assertEqual(settlement.status, "pending")
        self.assertEqual(wallet.pending_balance, Decimal("10000.00"))

    def test_settlement_creation_is_idempotent(self):
        user = User.objects.create_user(username="seller2", email="seller2@example.com", password="StrongPass123!")
        seller = SellerProfile.objects.create(user=user, business_name="Seller Shop 2")
        SellerWallet.objects.create(seller=seller)
        product = Product.objects.create(name="Test Item", sku="TEST-002", price=Decimal("500.00"), stock_quantity=5, seller=seller)
        order = Order.objects.create(customer_name="Buyer", email="buyer2@example.com", phone="254700000001", address="Nakuru", total_amount=Decimal("500.00"))
        OrderItem.objects.create(order=order, product=product, quantity=2, price=Decimal("250.00"), seller=seller, seller_gross=Decimal("500.00"), platform_commission=Decimal("25.00"), seller_net=Decimal("500.00"))

        _create_seller_settlements(order)
        _create_seller_settlements(order)

        self.assertEqual(SellerSettlement.objects.filter(order=order, seller=seller).count(), 1)
        self.assertEqual(SellerWallet.objects.get(seller=seller).pending_balance, Decimal("500.00"))


class DataIntegrityConstraintTests(TestCase):
    def test_product_price_cannot_be_zero(self):
        with self.assertRaises(IntegrityError):
            Product.objects.create(name="Invalid", sku="BAD-PRICE", price=Decimal("0.00"), stock_quantity=1)

    def test_product_discount_cannot_exceed_100(self):
        with self.assertRaises(IntegrityError):
            Product.objects.create(name="Invalid", sku="BAD-DISC", price=Decimal("100.00"), discount_percent=101, stock_quantity=1)

    def test_review_rating_must_be_between_one_and_five(self):
        user = User.objects.create_user(username="reviewer", email="reviewer@example.com", password="StrongPass123!")
        product = Product.objects.create(name="Review Item", sku="REVIEW-001", price=Decimal("100.00"), stock_quantity=1)
        order = Order.objects.create(customer_name="Reviewer", email="reviewer@example.com", phone="254700000002", address="Nairobi", total_amount=Decimal("100.00"))
        with self.assertRaises(IntegrityError):
            ProductReview.objects.create(product=product, customer=user, order=order, rating=6)

    def test_only_one_default_address_is_allowed(self):
        user = User.objects.create_user(username="addressuser", email="address@example.com", password="StrongPass123!")
        CustomerAddress.objects.create(user=user, label="Home", full_name="Buyer", phone="254700000003", county="Nairobi", town="Nairobi", address_line="One", is_default=True)
        with self.assertRaises(IntegrityError):
            CustomerAddress.objects.create(user=user, label="Office", full_name="Buyer", phone="254700000003", county="Nairobi", town="Nairobi", address_line="Two", is_default=True)



class CustomerOrderPrivacyTests(TestCase):
    def setUp(self):
        self.customer_a = User.objects.create_user(
            username="privacy_a", email="privacy-a@example.com", password="StrongPass123!"
        )
        self.customer_b = User.objects.create_user(
            username="privacy_b", email="privacy-b@example.com", password="StrongPass123!"
        )
        self.order_a = Order.objects.create(
            customer=self.customer_a, customer_name="Customer A", email=self.customer_a.email,
            phone="254700000001", address="A private address", total_amount=Decimal("250.00"),
        )
        self.order_b = Order.objects.create(
            customer=self.customer_b, customer_name="Customer B", email=self.customer_b.email,
            phone="254700000002", address="B private address", total_amount=Decimal("500.00"),
        )

    def test_customer_can_only_view_own_order_success(self):
        self.client.force_login(self.customer_a)
        response = self.client.get(reverse("order_success", args=[self.order_a.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A private address")
        response = self.client.get(reverse("order_success", args=[self.order_b.id]))
        self.assertEqual(response.status_code, 404)

    def test_email_change_does_not_expose_other_customer_orders(self):
        self.client.force_login(self.customer_a)
        self.customer_a.email = self.customer_b.email
        self.customer_a.save(update_fields=["email"])
        response = self.client.get(reverse("customer_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Order #{self.order_a.id}")
        self.assertNotContains(response, f"Order #{self.order_b.id}")
        response = self.client.get(reverse("customer_order_tracking", args=[self.order_b.id]))
        self.assertEqual(response.status_code, 404)

    def test_guest_order_success_requires_secret_access_token(self):
        self.client.logout()
        response = self.client.get(reverse("order_success", args=[self.order_a.id]))
        self.assertEqual(response.status_code, 404)
        response = self.client.get(
            reverse("order_success", args=[self.order_a.id]),
            {"token": str(self.order_a.access_token)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A private address")


class CommercialHardeningTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username="hardening_customer", email="hardening@example.com", password="StrongPass123!")
        self.seller_user = User.objects.create_user(username="hardening_seller", email="seller-hardening@example.com", password="StrongPass123!")
        self.seller = SellerProfile.objects.create(user=self.seller_user, business_name="Hardening Seller")

    def test_customer_cannot_change_account_email_directly(self):
        self.client.force_login(self.customer)
        response = self.client.post(reverse("customer_profile"), {"email": "attacker@example.com"})
        self.customer.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.customer.email, "hardening@example.com")

    def test_seller_product_toggle_requires_post(self):
        product = Product.objects.create(name="Hardening Product", price=Decimal("100.00"), stock_quantity=5, seller=self.seller)
        self.client.force_login(self.seller_user)
        response = self.client.get(reverse("seller_product_toggle", args=[product.id]))
        product.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(product.is_active)


class MpesaCallbackSafetyTests(TestCase):
    def _payment_fixture(self):
        user = User.objects.create_user(username="mpesabuyer", email="mpesa@example.com", password="StrongPass123!")
        product = Product.objects.create(name="M-PESA Item", sku="MPESA-001", price=Decimal("1000.00"), stock_quantity=2)
        order = Order.objects.create(customer_name="Buyer", email=user.email, phone="254712345678", address="Nairobi", total_amount=Decimal("1000.00"), payment_status="pending")
        OrderItem.objects.create(order=order, product=product, quantity=1, price=Decimal("1000.00"), seller_gross=Decimal("1000.00"), platform_commission=Decimal("0.00"), seller_net=Decimal("1000.00"))
        payment = PaymentTransaction.objects.create(order=order, method="mpesa", status="pending", provider="daraja", amount=Decimal("1000.00"), phone="254712345678", checkout_request_id="ws_CO_TEST_001", idempotency_key="MPESA-TEST-001")
        product.stock_quantity = 1
        product.save(update_fields=["stock_quantity"])
        return order, payment, user

    def test_success_callback_missing_receipt_does_not_mark_paid(self):
        order, payment, _ = self._payment_fixture()
        payload = {"Body": {"stkCallback": {"CheckoutRequestID": payment.checkout_request_id, "ResultCode": 0, "ResultDesc": "Success", "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 1000}, {"Name": "PhoneNumber", "Value": 254712345678}]}}}}
        response = self.client.post(reverse("mpesa_callback"), data=json.dumps(payload), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(order.payment_reference, "")

    def test_success_callback_amount_mismatch_does_not_mark_paid(self):
        order, payment, _ = self._payment_fixture()
        payload = {"Body": {"stkCallback": {"CheckoutRequestID": payment.checkout_request_id, "ResultCode": 0, "ResultDesc": "Success", "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 999}, {"Name": "MpesaReceiptNumber", "Value": "RCP123"}, {"Name": "PhoneNumber", "Value": 254712345678}]}}}}
        response = self.client.post(reverse("mpesa_callback"), data=json.dumps(payload), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(order.payment_status, "pending")




class DeliveryRegistrationTests(TestCase):
    def test_delivery_signup_creates_pending_agent_and_credentials(self):
        response = self.client.post(
            reverse("delivery_signup"),
            {
                "username": "new_rider",
                "email": "new-rider@example.com",
                "first_name": "New",
                "last_name": "Rider",
                "phone": "0712345678",
                "vehicle_type": "Motorbike",
                "vehicle_number": "KDA123A",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "delivery/signup_success.html")
        user = User.objects.get(username="new_rider")
        agent = DeliveryAgent.objects.get(user=user)
        self.assertTrue(user.check_password("StrongPass123!"))
        self.assertTrue(user.is_active)
        self.assertFalse(agent.is_active)
        self.assertEqual(agent.phone, "254712345678")
        self.assertEqual(agent.vehicle_type, "Motorbike")
        self.assertEqual(agent.vehicle_number, "KDA123A")

    def test_delivery_signup_rejects_duplicate_email(self):
        User.objects.create_user(
            username="existing_rider",
            email="existing@example.com",
            password="StrongPass123!",
        )
        form = DeliveryRegistrationForm(
            data={
                "username": "another_rider",
                "email": "EXISTING@example.com",
                "first_name": "Another",
                "last_name": "Rider",
                "phone": "0723456789",
                "vehicle_type": "Bicycle",
                "vehicle_number": "",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("already registered", str(form.errors["email"]))

    def test_delivery_signup_normalizes_and_rejects_duplicate_phone(self):
        User.objects.create_user(
            username="phone_rider",
            email="phone-rider@example.com",
            password="StrongPass123!",
        )
        DeliveryAgent.objects.create(user=User.objects.get(username="phone_rider"), phone="254700000123")
        form = DeliveryRegistrationForm(
            data={
                "username": "phone_rider_two",
                "email": "phone-rider-two@example.com",
                "first_name": "Phone",
                "last_name": "Rider",
                "phone": "+254700000123",
                "vehicle_type": "Car",
                "vehicle_number": "KDB000A",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("already linked", str(form.errors["phone"]))

    def test_pending_delivery_login_is_not_authorized(self):
        user = User.objects.create_user(
            username="pending_rider",
            email="pending-rider@example.com",
            password="StrongPass123!",
        )
        DeliveryAgent.objects.create(user=user, phone="254711111111", is_active=False)
        response = self.client.post(
            reverse("delivery_login"),
            {"username": "pending_rider", "password": "StrongPass123!"},
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(
            response,
            "Your staff application is registered and awaiting administrator verification.",
        )

class DeliveryGpsCertificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rider_cert", email="rider-cert@example.com", password="StrongPass123!"
        )
        self.agent = DeliveryAgent.objects.create(
            user=self.user, phone="254700000099", vehicle_type="Motorbike", is_active=True
        )

    def test_delivery_ping_requires_delivery_agent_and_records_gps(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("delivery_ping_location"),
            {"latitude": "-1.292100", "longitude": "36.821900", "accuracy": "8.5", "speed": "4.2", "heading": "90"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.current_latitude, Decimal("-1.292100"))
        self.assertEqual(self.agent.current_longitude, Decimal("36.821900"))
        self.assertIsNotNone(self.agent.last_location_at)
        self.assertEqual(DeliveryLocationPing.objects.filter(agent=self.agent).count(), 1)
        self.assertEqual(DeliveryLocationPing.objects.get(agent=self.agent).accuracy_meters, Decimal("8.50"))

    def test_delivery_ping_rejects_invalid_coordinates(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("delivery_ping_location"),
            {"latitude": "91", "longitude": "36.821900"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(DeliveryLocationPing.objects.count(), 0)

    def test_admin_delivery_locations_returns_latest_secure_feed(self):
        admin_user = User.objects.create_user(
            username="gps_admin", email="gps-admin@example.com", password="StrongPass123!", is_staff=True
        )
        self.client.force_login(self.user)
        self.client.post(
            reverse("delivery_ping_location"),
            {"latitude": "-1.292100", "longitude": "36.821900"},
        )
        self.client.force_login(admin_user)
        response = self.client.get(reverse("shopiva_admin:delivery_locations"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["agents"]), 1)
        self.assertEqual(payload["agents"][0]["id"], self.agent.id)
        self.assertEqual(payload["agents"][0]["latitude"], -1.2921)
        self.assertEqual(payload["agents"][0]["longitude"], 36.8219)

    def test_admin_delivery_locations_rejects_anonymous_access(self):
        response = self.client.get(reverse("shopiva_admin:delivery_locations"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("admin/login", response["Location"])

    def test_delivery_ping_is_rate_limited(self):
        self.client.force_login(self.user)
        fixed_now = timezone.now()
        with patch("home.delivery_app.timezone.now", return_value=fixed_now):
            DeliveryLocationPing.objects.create(
                agent=self.agent,
                latitude=Decimal("-1.292100"),
                longitude=Decimal("36.821900"),
            )
            second = self.client.post(
                reverse("delivery_ping_location"),
                {"latitude": "-1.292101", "longitude": "36.821901"},
            )
        self.assertEqual(second.status_code, 429)
        self.assertFalse(second.json()["ok"])
        self.assertEqual(DeliveryLocationPing.objects.filter(agent=self.agent).count(), 1)


class DeliveryLogoutSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rider_logout", email="rider-logout@example.com", password="StrongPass123!"
        )
        self.agent = DeliveryAgent.objects.create(user=self.user, is_active=True, status="available")

    def test_delivery_logout_requires_post(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("delivery_logout"))
        self.assertEqual(response.status_code, 405)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "available")

    def test_delivery_logout_post_marks_agent_offline_and_logs_out(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("delivery_logout"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "offline")


class DeliveryVerificationTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username="delivery_customer",
            email="delivery-customer@example.com",
            password="StrongPass123!",
        )
        self.rider_user = User.objects.create_user(
            username="delivery_rider",
            email="delivery-rider@example.com",
            password="StrongPass123!",
        )
        self.agent = DeliveryAgent.objects.create(
            user=self.rider_user, is_active=True, status="available"
        )
        seller_user = User.objects.create_user(
            username="delivery_seller",
            email="delivery-seller@example.com",
            password="StrongPass123!",
        )
        self.seller = SellerProfile.objects.create(user=seller_user, business_name="Delivery Seller")
        self.wallet = SellerWallet.objects.create(seller=self.seller)
        product = Product.objects.create(
            name="Delivery Item", sku="DELIVERY-VERIFY-001", price=Decimal("1000.00"),
            stock_quantity=2, seller=self.seller
        )
        self.order = Order.objects.create(
            customer=self.customer, customer_name="Delivery Buyer",
            email=self.customer.email, phone="254700000010", address="Nairobi",
            total_amount=Decimal("1000.00"), status="shipped", delivery_agent=self.agent,
        )
        self.order.ensure_delivery_confirmation_code()
        self.order.save(update_fields=["delivery_confirmation_code"])
        OrderItem.objects.create(
            order=self.order, product=product, quantity=1, price=Decimal("1000.00"),
            seller=self.seller, seller_gross=Decimal("1000.00"),
            platform_commission=Decimal("125.00"), seller_net=Decimal("1000.00"),
        )
        SellerSettlement.objects.create(
            order=self.order, seller=self.seller, gross_amount=Decimal("1000.00"),
            platform_commission=Decimal("125.00"), seller_amount=Decimal("1000.00"),
            status="pending",
        )

    def _wrong_code(self):
        code = int(self.order.delivery_confirmation_code)
        return str((code + 1) % 1000000).zfill(6)

    def test_wrong_code_does_not_deliver(self):
        self.client.force_login(self.rider_user)
        start = self.client.post(reverse("delivery_action", args=[self.order.id]), {"action": "start"})
        self.assertEqual(start.status_code, 200)
        self.order.refresh_from_db()
        response = self.client.post(
            reverse("delivery_action", args=[self.order.id]),
            {"action": "delivered", "code": self._wrong_code()},
        )
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "out_for_delivery")

    def test_correct_code_delivers_and_releases_settlement(self):
        self.client.force_login(self.rider_user)
        self.client.post(reverse("delivery_action", args=[self.order.id]), {"action": "start"})
        self.order.refresh_from_db()
        response = self.client.post(
            reverse("delivery_action", args=[self.order.id]),
            {"action": "delivered", "code": self.order.delivery_confirmation_code},
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.wallet.refresh_from_db()
        settlement = SellerSettlement.objects.get(order=self.order, seller=self.seller)
        self.assertEqual(self.order.status, "delivered")
        self.assertIsNotNone(self.order.delivered_at)
        self.assertEqual(settlement.status, "available")
        self.assertEqual(self.wallet.available_balance, Decimal("1000.00"))

    def test_code_is_not_returned_in_rider_status_feed(self):
        self.client.force_login(self.rider_user)
        self.client.post(reverse("delivery_action", args=[self.order.id]), {"action": "start"})
        payload = self.client.get(reverse("delivery_status")).json()
        self.assertNotIn("delivery_confirmation_code", payload["orders"][0])

    def test_verification_locks_after_five_bad_codes(self):
        self.client.force_login(self.rider_user)
        self.client.post(reverse("delivery_action", args=[self.order.id]), {"action": "start"})
        wrong = self._wrong_code()
        for _ in range(4):
            response = self.client.post(
                reverse("delivery_action", args=[self.order.id]),
                {"action": "delivered", "code": wrong},
            )
            self.assertEqual(response.status_code, 400)
        response = self.client.post(
            reverse("delivery_action", args=[self.order.id]),
            {"action": "delivered", "code": wrong},
        )
        self.assertEqual(response.status_code, 429)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivery_verification_locked_at)


class ReorderTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username="reorder_customer",
            email="reorder-customer@example.com",
            password="StrongPass123!",
        )
        self.product = Product.objects.create(
            name="Reorder Item", sku="REORDER-001", price=Decimal("1500.00"),
            stock_quantity=3, is_active=True,
        )
        self.order = Order.objects.create(
            customer=self.customer, customer_name="Repeat Buyer",
            email=self.customer.email, phone="254700000011", address="Nairobi",
            total_amount=Decimal("1500.00"), status="delivered",
        )
        OrderItem.objects.create(
            order=self.order, product=self.product, quantity=2, price=Decimal("1500.00")
        )

    def test_reorder_adds_available_items_to_cart(self):
        self.client.force_login(self.customer)
        response = self.client.post(reverse("reorder_order", args=[self.order.id]))
        self.assertRedirects(response, reverse("cart"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["cart"], {str(self.product.id): 2})

    def test_reorder_does_not_exceed_stock(self):
        self.product.stock_quantity = 1
        self.product.save(update_fields=["stock_quantity"])
        self.client.force_login(self.customer)
        self.client.post(reverse("reorder_order", args=[self.order.id]))
        self.assertEqual(self.client.session["cart"], {str(self.product.id): 1})


class CustomerDeliveryLocationPrivacyTests(TestCase):
    def setUp(self):
        self.customer_a = User.objects.create_user(
            username="map_a", email="map-a@example.com", password="StrongPass123!"
        )
        self.customer_b = User.objects.create_user(
            username="map_b", email="map-b@example.com", password="StrongPass123!"
        )
        self.order_a = Order.objects.create(
            customer=self.customer_a, customer_name="A", email=self.customer_a.email,
            phone="254700000012", address="A address", total_amount=Decimal("100.00")
        )
        self.order_b = Order.objects.create(
            customer=self.customer_b, customer_name="B", email=self.customer_b.email,
            phone="254700000013", address="B address", total_amount=Decimal("100.00")
        )

    def test_customer_cannot_query_another_customer_order_location(self):
        self.client.force_login(self.customer_a)
        response = self.client.get(
            reverse("customer_delivery_location"), {"order_id": self.order_b.id}
        )
        self.assertEqual(response.status_code, 404)


class DeliveryAssignmentGuardTests(TestCase):
    def test_seller_cannot_send_order_out_for_delivery_without_rider(self):
        seller_user = User.objects.create_user(
            username="seller_guard", email="seller-guard@example.com",
            password="StrongPass123!",
        )
        seller = SellerProfile.objects.create(user=seller_user, business_name="Guard Seller")
        customer = User.objects.create_user(
            username="guard_customer", email="guard-customer@example.com",
            password="StrongPass123!",
        )
        product = Product.objects.create(
            name="Guard Item", sku="GUARD-001", price=Decimal("250.00"),
            stock_quantity=2, seller=seller
        )
        order = Order.objects.create(
            customer=customer, customer_name="Guard Buyer", email=customer.email,
            phone="254700000014", address="Nairobi", total_amount=Decimal("250.00"),
            status="shipped"
        )
        OrderItem.objects.create(
            order=order, product=product, quantity=1, price=Decimal("250.00"),
            seller=seller, seller_gross=Decimal("250.00"),
            platform_commission=Decimal("25.00"), seller_net=Decimal("250.00")
        )
        self.client.force_login(seller_user)
        response = self.client.post(
            reverse("seller_order_update", args=[order.id]),
            {"status": "out_for_delivery"},
        )
        order.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(order.status, "shipped")


class AdminPortalBoundaryTests(TestCase):
    def setUp(self):
        self.password = "StrongPass123!"
        self.admin = User.objects.create_user(
            username="strict_admin",
            email="strict-admin@example.com",
            password=self.password,
            is_staff=True,
        )

    def test_admin_is_redirected_away_from_customer_storefront(self):
        self.client.force_login(self.admin)
        for url_name in ("home", "products", "cart", "customer_login", "customer_dashboard"):
            response = self.client.get(reverse(url_name), secure=True)
            self.assertRedirects(response, "/admin/", fetch_redirect_response=False)

    def test_admin_is_redirected_away_from_seller_portal(self):
        self.client.force_login(self.admin)
        for url_name in ("seller_login", "seller_dashboard"):
            response = self.client.get(reverse(url_name), secure=True)
            self.assertRedirects(response, "/admin/", fetch_redirect_response=False)

    def test_admin_can_still_reach_admin_control_center(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("shopiva_admin:index"), secure=True)
        self.assertNotIn(response.status_code, (301, 302))

    def test_admin_logout_clears_session_and_returns_to_admin_login(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("customer_logout"), secure=True)
        self.assertRedirects(response, reverse("admin_login"), fetch_redirect_response=False)
        response = self.client.get(reverse("customer_dashboard"), secure=True)
        self.assertRedirects(response, f"{reverse('customer_login')}?next=%2Faccount%2F", fetch_redirect_response=False)

class MpesaCheckoutNavigationTests(TestCase):
    def test_successful_mpesa_checkout_redirects_to_waiting_page(self):
        order = Order.objects.create(
            customer_name="Buyer",
            email="buyer@example.com",
            phone="254712345678",
            address="Nairobi",
            total_amount=Decimal("1000.00"),
        )
        waiting_url = reverse("mpesa_waiting", args=[order.id])
        nested_response = HttpResponseRedirect(waiting_url)
        nested_response["X-Nested-Checkout-Response"] = "yes"

        with patch("home.checkout_map.original_checkout_mpesa", return_value=nested_response):
            response = self.client.post(
                reverse("checkout"),
                {
                    "email": order.email,
                    "delivery_latitude": "-1.292100",
                    "delivery_longitude": "36.821900",
                },
                secure=True,
            )

        self.assertRedirects(response, waiting_url, fetch_redirect_response=False)
        self.assertNotIn("X-Nested-Checkout-Response", response)
        order.refresh_from_db()
        self.assertEqual(order.delivery_latitude, Decimal("-1.292100"))
        self.assertEqual(order.delivery_longitude, Decimal("36.821900"))
