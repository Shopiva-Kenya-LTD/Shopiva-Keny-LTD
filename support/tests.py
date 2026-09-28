from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from home.models import SellerProfile

from .models import SupportMessage, SupportTicket

User = get_user_model()


class SupportCenterTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username="support-customer", password="StrongPass123!")
        self.seller = User.objects.create_user(username="support-seller", password="StrongPass123!")
        SellerProfile.objects.create(user=self.seller, business_name="Support Seller", is_active=True)
        self.staff = User.objects.create_user(username="support-staff", password="StrongPass123!", is_staff=True)

    def test_customer_can_open_case_and_reply(self):
        self.client.force_login(self.customer)
        response = self.client.post(reverse("support_center"), {
            "action": "new",
            "subject": "Payment question",
            "category": "Payment & M-PESA",
            "priority": "high",
            "order_reference": "SPV-TEST-1",
            "body": "I need help confirming my payment.",
        })
        self.assertEqual(response.status_code, 302)
        ticket = SupportTicket.objects.get(user=self.customer)
        self.assertEqual(ticket.role, "customer")
        self.assertEqual(ticket.priority, "high")
        self.assertEqual(ticket.status, "open")
        self.assertIsNone(ticket.last_response_at)
        self.assertEqual(ticket.messages.count(), 1)

        response = self.client.post(reverse("support_center"), {
            "action": "reply",
            "ticket_id": str(ticket.id),
            "body": "Here is an additional detail.",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticket.messages.count(), 2)

    def test_seller_is_identified_as_seller(self):
        self.client.force_login(self.seller)
        response = self.client.post(reverse("support_center"), {
            "action": "new",
            "subject": "Seller payout help",
            "category": "Seller Payout",
            "priority": "normal",
            "body": "Please help me understand my payout status.",
        })
        self.assertEqual(response.status_code, 302)
        ticket = SupportTicket.objects.get(user=self.seller)
        self.assertEqual(ticket.role, "seller")

    def test_staff_can_reply_and_update_status(self):
        ticket = SupportTicket.objects.create(
            user=self.customer,
            role="customer",
            subject="Delivery issue",
            category="Delivery",
        )
        SupportMessage.objects.create(ticket=ticket, author=self.customer, body="My order is late.")
        self.client.force_login(self.staff)

        response = self.client.post(reverse("support_admin_center"), {
            "action": "reply",
            "ticket_id": str(ticket.id),
            "body": "Shopiva Support is reviewing your case.",
        })
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "waiting_for_customer")
        self.assertEqual(ticket.messages.filter(from_staff=True).count(), 1)

        response = self.client.post(reverse("support_admin_center"), {
            "action": "status",
            "ticket_id": str(ticket.id),
            "status": "resolved",
        })
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "resolved")

    def test_resolved_cases_leave_active_queue_but_remain_in_history(self):
        resolved = SupportTicket.objects.create(
            user=self.customer,
            role="customer",
            subject="Resolved delivery issue",
            category="Delivery",
            status="resolved",
        )
        active = SupportTicket.objects.create(
            user=self.customer,
            role="customer",
            subject="Active delivery issue",
            category="Delivery",
            status="open",
        )
        self.client.force_login(self.staff)

        response = self.client.get(reverse("support_admin_center"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active delivery issue")
        self.assertNotContains(response, "Resolved delivery issue")

        response = self.client.get(reverse("support_admin_center") + "?status=resolved")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Resolved delivery issue")
        self.assertNotContains(response, "Active delivery issue")
        self.assertTrue(SupportTicket.objects.filter(id=resolved.id).exists())

    def test_staff_can_delete_only_resolved_or_closed_cases(self):
        resolved = SupportTicket.objects.create(
            user=self.customer,
            role="customer",
            subject="Resolved case to delete",
            category="General",
            status="resolved",
        )
        active = SupportTicket.objects.create(
            user=self.customer,
            role="customer",
            subject="Active case to protect",
            category="General",
            status="open",
        )
        SupportMessage.objects.create(
            ticket=resolved,
            author=self.customer,
            body="Case history should be removed with the case.",
        )
        self.client.force_login(self.staff)

        response = self.client.post(reverse("support_admin_center"), {
            "action": "delete",
            "ticket_id": str(active.id),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SupportTicket.objects.filter(id=active.id).exists())

        response = self.client.post(reverse("support_admin_center"), {
            "action": "delete",
            "ticket_id": str(resolved.id),
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SupportTicket.objects.filter(id=resolved.id).exists())
        self.assertFalse(SupportMessage.objects.filter(ticket_id=resolved.id).exists())

    def test_closed_case_rejects_customer_reply(self):
        ticket = SupportTicket.objects.create(
            user=self.customer,
            role="customer",
            subject="Closed issue",
            category="General",
            status="closed",
        )
        self.client.force_login(self.customer)
        response = self.client.post(reverse("support_center"), {
            "action": "reply",
            "ticket_id": str(ticket.id),
            "body": "Trying to reply to a closed case.",
        })
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.messages.count(), 0)
        self.assertEqual(ticket.status, "closed")

    def test_closed_case_rejects_staff_reply(self):
        ticket = SupportTicket.objects.create(
            user=self.customer,
            role="customer",
            subject="Closed staff case",
            category="General",
            status="closed",
        )
        self.client.force_login(self.staff)
        response = self.client.post(reverse("support_admin_center"), {
            "action": "reply",
            "ticket_id": str(ticket.id),
            "body": "Trying to reply to a closed case.",
        })
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.messages.count(), 0)
        self.assertEqual(ticket.status, "closed")

    def test_non_staff_cannot_open_admin_support_center(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse("support_admin_center"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)
