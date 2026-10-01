"""Small, deployment-independent response hardening controls."""


class ShopivaSecurityHeadersMiddleware:
    """Add headers Django does not provide as settings in every supported version.

    The policy deliberately does not use a restrictive CSP here: the existing
    application contains inline scripts and third-party map/media resources.
    CSP remains in report-only mode until the documented rollout inventory is
    completed, rather than breaking checkout or staff operations in production.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault(
            "Permissions-Policy",
            "geolocation=(self), camera=(), microphone=(self), payment=(), usb=(), browsing-topics=()",
        )
        return response
