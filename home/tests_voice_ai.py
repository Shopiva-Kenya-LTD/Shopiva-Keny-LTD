import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase

from .models import DeliveryAgent


class NiaVoiceSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="voice_user",
            email="voice@example.com",
            password="StrongPass123!",
        )
        self.client = Client()

    def tearDown(self):
        cache.clear()

    def test_voice_provider_endpoints_require_authentication(self):
        endpoints = (
            ("/ai/realtime/call/", {"data": "v=0\\r\\n", "content_type": "application/sdp"}),
            ("/ai/voice/transcribe/", {}),
            ("/ai/voice/speak/", {"data": {"text": "hello"}}),
        )
        for path, kwargs in endpoints:
            with self.subTest(path=path):
                response = self.client.post(path, **kwargs)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["ok"], False)

    def test_realtime_call_uses_csrf_protection(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        response = csrf_client.post(
            "/ai/realtime/call/",
            data="v=0\\r\\n",
            content_type="application/sdp",
        )
        self.assertEqual(response.status_code, 403)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False)
    @patch("home.voice_ai._openai_multipart_sdp", return_value="v=0\\r\\n")
    def test_realtime_call_is_rate_limited(self, _mock_sdp):
        self.client.force_login(self.user)
        for _ in range(5):
            response = self.client.post(
                "/ai/realtime/call/",
                data="v=0\\r\\n",
                content_type="application/sdp",
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/ai/realtime/call/",
            data="v=0\\r\\n",
            content_type="application/sdp",
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_delivery_accounts_are_not_treated_as_customer_voice_accounts(self):
        delivery_user = User.objects.create_user(
            username="voice_rider",
            email="rider@example.com",
            password="StrongPass123!",
        )
        DeliveryAgent.objects.create(
            user=delivery_user,
            phone="254712345678",
            vehicle_type="Motorbike",
            vehicle_number="KDA123A",
            is_active=True,
        )
        self.client.force_login(delivery_user)
        response = self.client.post("/ai/voice/speak/", {"text": "hello"})
        self.assertEqual(response.status_code, 403)
