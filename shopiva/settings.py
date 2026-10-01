import os
import secrets
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.getenv("DEBUG", "false").lower() == "true"
secret_key = os.getenv("SECRET_KEY", "").strip()
if not secret_key:
    if DEBUG:
        secret_key = secrets.token_urlsafe(50)
    else:
        raise RuntimeError("SECRET_KEY is required. Set it in the deployment environment.")
SECRET_KEY = secret_key

_default_hosts = "shopivakenya.top,www.shopivakenya.top,shopiva-keny-ltd.onrender.com,localhost,127.0.0.1,testserver"
ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS", _default_hosts).split(",") if host.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "cloudinary",
    "home",
    "support",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "axes.middleware.AxesMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "home.security_middleware.ShopivaSecurityHeadersMiddleware",
    "home.admin_portal_boundary.ShopivaAdminPortalBoundaryMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "home.homepage_cache_buster.ShopivaHomepageCacheBusterMiddleware",
    "home.seo_middleware.ShopivaSeoMiddleware",
    "home.branding_middleware.ShopivaBrandingMiddleware",
]

ROOT_URLCONF = "shopiva.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Application templates are intentional overrides.  In particular,
        # the Product Creation Studio lives in home/templates and must win
        # over legacy project-level admin/product templates.
        "DIRS": [BASE_DIR / "home" / "templates", BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "home.context_processors.google_maps",
            ],
        },
    },
]

WSGI_APPLICATION = "shopiva.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        os.environ["DATABASE_URL"],
        conn_max_age=60,
    )
}
# Neon/Postgres connections can be rotated or dropped while Render workers stay alive.
# Health checks prevent Django from reusing a stale connection.
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"], "ip_address"]
AXES_ENABLE_ACCESS_FAILURE_LOG = True
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ADMIN = True

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

EMAIL_NOTIFICATIONS_ENABLED = os.getenv("EMAIL_NOTIFICATIONS_ENABLED", "false").lower() == "true"
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.resend.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "resend")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Shopiva <no-reply@shopiva.co.ke>")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "20"))

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
GOOGLE_MAPS_MAP_ID = os.getenv("GOOGLE_MAPS_MAP_ID", "").strip()

PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "https://shopivakenya.top").strip().rstrip("/")
INDEXNOW_KEY = os.getenv("INDEXNOW_KEY", "").strip()

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CSRF_TRUSTED_ORIGINS",
        "https://shopivakenya.top,https://www.shopivakenya.top,https://shopiva-keny-ltd.onrender.com",
    ).split(",")
    if origin.strip()
]
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_RESOURCE_POLICY = "same-origin"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CSP_REPORT_ONLY = {
    "default-src": ["'self'"],
    "base-uri": ["'self'"],
    "object-src": ["'none'"],
    "frame-ancestors": ["'none'"],
    "img-src": ["'self'", "data:", "https:", "blob:"],
    "font-src": ["'self'", "https:", "data:"],
    "style-src": ["'self'", "'unsafe-inline'", "https:"],
    "script-src": ["'self'", "'unsafe-inline'", "https:"],
    "connect-src": ["'self'", "https:"],
    "frame-src": ["'self'", "https:"],
    "media-src": ["'self'", "https:", "blob:"],
    "worker-src": ["'self'", "blob:"],
    "manifest-src": ["'self'"],
    "form-action": ["'self'", "https:"],
}


if not DEBUG:
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "true").lower() == "true"
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    # Do not preload without an external DNS/TLS audit of every present and
    # future subdomain. HSTS still protects this application and www.
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "false").lower() == "true"
    SECURE_HSTS_PRELOAD = False
else:
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

SMS_NOTIFICATIONS_ENABLED = os.getenv("SMS_NOTIFICATIONS_ENABLED", "false").lower() == "true"
AFRICASTALKING_USERNAME = os.getenv("AFRICASTALKING_USERNAME", "")
AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY", "")
AFRICASTALKING_SENDER_ID = os.getenv("AFRICASTALKING_SENDER_ID", "")
WHATSAPP_NOTIFICATIONS_ENABLED = os.getenv("WHATSAPP_NOTIFICATIONS_ENABLED", "false").lower() == "true"
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")
WHATSAPP_TEMPLATE_NAME = os.getenv("WHATSAPP_TEMPLATE_NAME", "")
WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")

MPESA_ENV = os.getenv("MPESA_ENV", "sandbox").strip().lower()
MPESA_CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE = os.getenv("MPESA_SHORTCODE", "")
MPESA_TILL_NUMBER = os.getenv("MPESA_TILL_NUMBER", "")
MPESA_PASSKEY = os.getenv("MPESA_PASSKEY", "")
MPESA_CALLBACK_URL = os.getenv("MPESA_CALLBACK_URL", "https://shopivakenya.top/payments/mpesa/callback/")

# Co-operative Bank Co-op Connect SIT. Credentials stay in the deployment secret store.
COOP_CONNECT_SIT_BASE_URL = os.getenv("COOP_CONNECT_SIT_BASE_URL", "https://openapi-sit.co-opbank.co.ke").strip().rstrip("/")
COOP_CONNECT_SIT_CLIENT_ID = os.getenv("COOP_CONNECT_SIT_CLIENT_ID", "").strip()
COOP_CONNECT_SIT_CLIENT_SECRET = os.getenv("COOP_CONNECT_SIT_CLIENT_SECRET", "").strip()
COOP_CONNECT_SIT_USER_ID = os.getenv("COOP_CONNECT_SIT_USER_ID", "").strip()
COOP_CONNECT_SIT_OPERATOR_CODE = os.getenv("COOP_CONNECT_SIT_OPERATOR_CODE", "").strip()
COOP_CONNECT_SIT_CALLBACK_URL = os.getenv("COOP_CONNECT_SIT_CALLBACK_URL", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()

# Accept either Cloudinary's combined CLOUDINARY_URL or its three-part credentials.
# Ignore malformed combined values so a mistaken API-key-only value cannot poison startup.
if all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
    import cloudinary
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )
elif not CLOUDINARY_URL.startswith("cloudinary://"):
    CLOUDINARY_URL = ""

# Production static delivery without manifest coupling.
WHITENOISE_MAX_AGE = 31536000
# Keep existing Render deployments able to serve Django staticfiles even before a build refreshes STATIC_ROOT.
WHITENOISE_USE_FINDERS = True
