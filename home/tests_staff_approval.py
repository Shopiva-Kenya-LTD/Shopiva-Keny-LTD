from django.contrib.auth.models import User
from django.test import TestCase
from unittest.mock import patch
from django.urls import reverse

from .forms import DeliveryRegistrationForm
from .models import DeliveryAgent, Notification
from .notification_service import notify_user as real_notify_user


class DeliveryStaffApprovalFlowTests(TestCase):
    def setUp(self):
        self.password = "StrongPass123!"
        self.admin = User.objects.create_user(
            username="approval_admin",
            email="admin@example.com",
            password=self.password,
            is_staff=True,
            is_active=True,
        )

    def application_data(self, suffix="1"):
        return {
            "username": f"rider{suffix}",
            "email": f"rider{suffix}@example.com",
            "first_name": "Test",
            "last_name": "Rider",
            "phone": f"07123456{suffix}0",
            "vehicle_type": "Motorbike",
            "vehicle_number": f"KDA {suffix}01 AB",
            "password1": self.password,
            "password2": self.password,
        }

    @patch("home.notification_service.notify_user", wraps=real_notify_user)
    def test_new_delivery_application_notifies_active_admin(self, notify):
        form = DeliveryRegistrationForm(data=self.application_data("2"))
        self.assertTrue(form.is_valid(), form.errors.as_text())
        user = form.save()

        agent = DeliveryAgent.objects.get(user=user)
        self.assertFalse(agent.is_active)
        self.assertTrue(
            Notification.objects.filter(
                user=self.admin,
                title="New staff approval required",
                link=reverse("shopiva_admin:approval_center"),
            ).exists()
        )

    def test_inactive_delivery_applicant_cannot_self_activate_without_an_active_admin(self):
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        user = User.objects.create_user(
            username="inactive_rider",
            email="inactive@example.com",
            password=self.password,
            is_active=True,
        )
        agent = DeliveryAgent.objects.create(
            user=user,
            phone="254712345679",
            vehicle_type="Motorbike",
            vehicle_number="KDA 124A",
            status="offline",
            is_active=False,
        )

        response = self.client.post(
            reverse("delivery_login"),
            {"username": user.username, "password": self.password},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Your staff application is registered and awaiting administrator verification.",
        )
        agent.refresh_from_db()
        self.assertFalse(agent.is_active)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_approval_center_resolves_to_the_custom_admin_site(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("shopiva_admin:approval_center"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New staff approval queue")

    @patch("home.admin_operations.notify_user", wraps=real_notify_user)
    def test_admin_approval_activates_staff_and_notifies_applicant(self, notify):
        user = User.objects.create_user(
            username="pending_rider",
            email="pending@example.com",
            password=self.password,
            is_active=True,
        )
        agent = DeliveryAgent.objects.create(
            user=user,
            phone="254712345678",
            vehicle_type="Motorbike",
            vehicle_number="KDA 123A",
            status="offline",
            is_active=False,
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("shopiva_admin:approval_center"),
            {
                "action": "approve_delivery",
                "agent_id": str(agent.id),
                "verification_confirmed": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        agent.refresh_from_db()
        user.refresh_from_db()
        self.assertTrue(agent.is_active)
        self.assertTrue(user.is_active)
        self.assertTrue(
            Notification.objects.filter(
                user=user,
                title="Delivery staff application approved",
                link="/delivery/login/",
            ).exists()
        )
        self.assertTrue(notify.called)
