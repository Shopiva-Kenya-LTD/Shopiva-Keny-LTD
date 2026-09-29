from django.shortcuts import redirect


class ShopivaAdminPortalBoundaryMiddleware:
    """Keep staff/admin identities inside the Shopiva Admin Control Center.

    Admin/staff accounts are records/operations identities, not customer or
    seller identities. They may only render the admin portal; public assets,
    health checks and provider callbacks remain reachable because those routes
    are not customer/seller application surfaces.
    """

    _ADMIN_PREFIXES = (
        "/admin/",
        "/admin",
    )

    _EXACT_ALLOWED = {
        "/health/",
        "/robots.txt",
        "/sitemap.xml",
        "/merchant-feed.xml",
        "/favicon.ico",
        "/app-icon.svg",
        "/service-worker.js",
        "/customer/logout/",
        "/seller/logout/",
        "/delivery/logout/",
        "/payments/mpesa/callback/",
        "/payments/pesapal/ipn/",
        "/payments/stripe/webhook/",
        "/payments/mpesa/b2c/result/",
        "/payments/mpesa/b2c/timeout/",
    }

    _ALLOWED_PREFIXES = (
        "/static/",
        "/media/",
        # Admin-only Nia phone endpoints may be called by the Control Center.
        "/ai/phone/",
        # Browser Nia Live Copilot endpoints used by the Admin Control Center.
        "/ai/realtime/",
        "/ai/nia/dashboard-context/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and (user.is_staff or user.is_superuser):
            path = request.path
            allowed = (
                path in self._EXACT_ALLOWED
                or path.startswith(self._ALLOWED_PREFIXES)
                or path.startswith(self._ADMIN_PREFIXES)
            )
            if not allowed:
                return redirect("/admin/")
        return self.get_response(request)
