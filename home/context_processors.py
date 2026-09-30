import os
from django.conf import settings


def google_maps(request):
    # Only expose readiness booleans to templates; never expose payment secrets.
    mpesa_ready = (
        os.getenv("MPESA_ENV", "sandbox").strip().lower() == "production"
        and all(os.getenv(name, "").strip() for name in (
            "MPESA_CONSUMER_KEY",
            "MPESA_CONSUMER_SECRET",
            "MPESA_PASSKEY",
            "MPESA_CALLBACK_URL",
            "MPESA_TILL_NUMBER",
        ))
    )
    return {
        "google_maps_api_key": getattr(settings, "GOOGLE_MAPS_API_KEY", ""),
        "google_maps_map_id": getattr(settings, "GOOGLE_MAPS_MAP_ID", ""),
        "mpesa_ready": mpesa_ready,
    }
