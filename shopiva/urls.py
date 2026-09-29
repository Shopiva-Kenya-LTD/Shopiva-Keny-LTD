from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from home.admin import shopiva_admin_site
from home.admin_helpers import admin_login, admin_logout
from home.admin_session import admin_session_status
from home.business_intelligence import business_intelligence
from home.ai import shop_assistant
from home.role_ai import seller_assistant
from home.customer_tracking import customer_order_tracking
from home.checkout_map import checkout_mpesa_map, checkout_quote, pickup_points
from home.location_views import shopiva_locations
from home.google_admin_map import google_admin_delivery_map
from home.map_views import customer_addresses_map, customer_delivery_location_map, seller_product_add_map, seller_product_edit_map
from home.platform import app_install, app_manifest, service_worker, favicon, shopiva_app_icon
from home.payments import (
    mpesa_callback, mpesa_payment_status, mpesa_payment_verify, mpesa_waiting,
    pesapal_callback, pesapal_ipn, pesapal_cancel,
    card_payment_success, card_payment_cancel, stripe_webhook,
)
from home.seller_auth import seller_login, seller_logout, seller_login_required
from home.voice_ai import realtime_action, realtime_call, speak_text, transcribe_voice
from home.nia_phone import start_nia_call, nia_phone_answer, nia_phone_respond, nia_phone_status, nia_phone_incoming, nia_phone_verify, nia_phone_pin
from home.nia_tasks import run_nia_tasks_endpoint
from home.nia_admin import nia_dashboard_context
from shopiva.health import health
from home.delivery_app import delivery_login, delivery_signup, delivery_logout, delivery_action, delivery_status, delivery_history, delivery_payouts, delivery_b2c_result, delivery_b2c_timeout, delivery_update_location, delivery_ping_location
from home.delivery_platform import delivery_manifest, delivery_service_worker
from home.admin_delivery_feed import admin_live_delivery_feed
from home.admin_statistics import statistics_center
from home.coop_connect_views import coop_connect_sit_console
from home.notifications_center import customer_notification_center, seller_notification_center, admin_notification_center
from home.views import add_to_cart, cart, categories, contact_center, customer_dashboard, customer_login, customer_logout, customer_orders, customer_profile, customer_register, customer_wishlist, reorder_order, seller_dashboard, seller_product_toggle, seller_product_delete, seller_product_stock_update, seller_order_update, product_review, seller_register, seller_request_payout, delivery_portal, home, order_success, product_detail, products
from home.seo import robots_txt, sitemap_xml
from home.indexnow import indexnow_key
from home.legal import privacy_policy, terms_of_service, account_deletion
from home.merchant_feed import merchant_feed_xml
from home.shipping_policy import shipping_policy
from home.returns_policy import returns_policy
from support.views import support_admin_center, support_center, system_issues_center

urlpatterns = [
    path("", home, name="home"),
    path("health/", health, name="health"),
    path("robots.txt", robots_txt, name="robots_txt"),
    path("<str:key>.txt", indexnow_key, name="indexnow_key"),
    path("sitemap.xml", sitemap_xml, name="sitemap_xml"),
    path("merchant-feed.xml", merchant_feed_xml, name="merchant_feed_xml"),
    path("privacy/", privacy_policy, name="privacy_policy"),
    path("terms/", terms_of_service, name="terms_of_service"),
    path("shipping/", shipping_policy, name="shipping_policy"),
    path("returns/", returns_policy, name="returns_policy"),
    path("contact/", contact_center, name="contact_center"),
    path("account/delete/", account_deletion, name="account_deletion"),
    path("favicon.ico", favicon, name="favicon"),
    path("app-icon.svg", shopiva_app_icon, name="shopiva_app_icon"),
    path("products/", products, name="products"),
    path("categories/", categories, name="categories"),
    path("product/<int:product_id>/", product_detail, name="product_detail"),
    path("install/", app_install, name="app_install"),
    path("manifest.webmanifest", app_manifest, name="manifest_webmanifest"),
    path("service-worker.js", service_worker, name="service_worker"),
    path("cart/", cart, name="cart"),
    path("cart/add/<int:product_id>/", add_to_cart, name="add_to_cart"),

    path("checkout/", checkout_mpesa_map, name="checkout"),
    path("checkout/quote/", checkout_quote, name="checkout_quote"),
    path("checkout/pickup-points/", pickup_points, name="pickup_points"),
    path("api/locations/", shopiva_locations, name="shopiva_locations"),
    path("payments/mpesa/callback/", mpesa_callback, name="mpesa_callback"),
    path("payments/mpesa/status/<int:order_id>/", mpesa_payment_status, name="mpesa_payment_status"),
    path("payments/mpesa/verify/<int:order_id>/", mpesa_payment_verify, name="mpesa_payment_verify"),
    path("payments/mpesa/waiting/<int:order_id>/", mpesa_waiting, name="mpesa_waiting"),
    path("payments/pesapal/callback/", pesapal_callback, name="pesapal_callback"),
    path("payments/pesapal/ipn/", pesapal_ipn, name="pesapal_ipn"),
    path("payments/pesapal/cancel/", pesapal_cancel, name="pesapal_cancel"),
    path("payments/card/success/<int:order_id>/", card_payment_success, name="card_payment_success"),
    path("payments/card/cancel/<int:order_id>/", card_payment_cancel, name="card_payment_cancel"),
    path("payments/stripe/webhook/", stripe_webhook, name="stripe_webhook"),
    path("order-success/<int:order_id>/", order_success, name="order_success"),

    path("support/", support_center, name="support_center"),
    path("admin/support-center/", support_admin_center, name="support_admin_center"),
    path("admin/system-issues/", system_issues_center, name="system_issues_center"),

    path("seller/notifications/", seller_notification_center, name="seller_notifications"),
    path("notifications/", customer_notification_center, name="notification_center"),

    path("seller/register/", seller_register, name="seller_register"),
    path("seller/login/", seller_login, name="seller_login"),
    path("seller/logout/", seller_logout, name="seller_logout"),
    path("seller/", seller_login_required(seller_dashboard), name="seller_dashboard"),
    path("seller/products/add/", seller_login_required(seller_product_add_map), name="seller_product_add"),
    path("seller/products/<int:product_id>/edit/", seller_login_required(seller_product_edit_map), name="seller_product_edit"),
    path("seller/products/<int:product_id>/toggle/", seller_login_required(seller_product_toggle), name="seller_product_toggle"),
    path("seller/products/<int:product_id>/stock/", seller_login_required(seller_product_stock_update), name="seller_product_stock_update"),
    path("seller/orders/<int:order_id>/update/", seller_login_required(seller_order_update), name="seller_order_update"),
    path("seller/products/<int:product_id>/hide/", seller_login_required(seller_product_delete), name="seller_product_delete"),
    path("seller/payout/request/", seller_login_required(seller_request_payout), name="seller_request_payout"),

    path("customer/register/", customer_register, name="customer_register"),
    path("customer/login/", customer_login, name="customer_login"),
    path("customer/logout/", customer_logout, name="customer_logout"),
    path("account/", customer_dashboard, name="customer_dashboard"),
    path("account/orders/", customer_orders, name="customer_orders"),
    path("account/orders/<int:order_id>/", customer_order_tracking, name="customer_order_tracking"),
    path("account/orders/<int:order_id>/reorder/", reorder_order, name="reorder_order"),
    path("account/profile/", customer_profile, name="customer_profile"),
    path("account/addresses/", customer_addresses_map, name="customer_addresses"),
    path("account/wishlist/", customer_wishlist, name="customer_wishlist"),
    path("account/notifications/", customer_notification_center, name="customer_notifications"),
    path("product/<int:product_id>/review/", product_review, name="product_review"),
    path("account/delivery-location/", customer_delivery_location_map, name="customer_delivery_location"),

    path("delivery/login/", delivery_login, name="delivery_login"),
    path("delivery/signup/", delivery_signup, name="delivery_signup"),
    path("delivery/logout/", delivery_logout, name="delivery_logout"),
    path("delivery/", delivery_portal, name="delivery_portal"),
    path("delivery/history/", delivery_history, name="delivery_history"),
    path("delivery/payouts/", delivery_payouts, name="delivery_payouts"),
    path("payments/mpesa/b2c/result/", delivery_b2c_result, name="delivery_b2c_result"),
    path("payments/mpesa/b2c/timeout/", delivery_b2c_timeout, name="delivery_b2c_timeout"),
    path("delivery/manifest.webmanifest", delivery_manifest, name="delivery_manifest"),
    path("delivery/service-worker.js", delivery_service_worker, name="delivery_service_worker"),
    path("delivery/order/<int:order_id>/action/", delivery_action, name="delivery_action"),
    path("delivery/status/", delivery_status, name="delivery_status"),
    path("delivery/location/", delivery_update_location, name="delivery_update_location"),
    path("delivery/location/ping/", delivery_ping_location, name="delivery_ping_location"),

    path("admin/business-intelligence/", business_intelligence, name="business_intelligence"),
    path("admin/google-delivery-map/", google_admin_delivery_map, name="google_delivery_map"),
    path("admin/live-delivery-feed/", admin_live_delivery_feed, name="admin_live_delivery_feed"),
    path("admin/notifications/", admin_notification_center, name="admin_notifications"),
    path("admin/statistics/", statistics_center, name="statistics_center"),
    path("admin/coop-connect/sit/", coop_connect_sit_console, name="coop_connect_sit_console"),
    path("ai/shop-assistant/", shop_assistant, name="shop_assistant"),
    path("ai/seller-assistant/", seller_assistant, name="seller_assistant"),
    path("ai/voice/transcribe/", transcribe_voice, name="voice_transcribe"),
    path("ai/realtime/call/", realtime_call, name="realtime_call"),
    path("ai/realtime/action/", realtime_action, name="realtime_action"),
    path("ai/voice/speak/", speak_text, name="voice_speak"),
    path("ai/phone/call/", start_nia_call, name="nia_phone_call"),
    path("ai/phone/pin/", nia_phone_pin, name="nia_phone_pin"),
    path("ai/phone/incoming/", nia_phone_incoming, name="nia_phone_incoming"),
    path("ai/phone/verify/<uuid:session_id>/", nia_phone_verify, name="nia_phone_verify"),
    path("ai/phone/answer/<uuid:session_id>/", nia_phone_answer, name="nia_phone_answer"),
    path("ai/phone/respond/<uuid:session_id>/", nia_phone_respond, name="nia_phone_respond"),
    path("ai/phone/status/<uuid:session_id>/", nia_phone_status, name="nia_phone_status"),
    path("internal/run-nia-tasks/", run_nia_tasks_endpoint, name="run_nia_tasks_endpoint"),
    path("ai/nia/dashboard-context/", nia_dashboard_context, name="nia_dashboard_context"),

    path("admin/login/", admin_login, name="admin_login"),
    path("admin/logout/", admin_logout, name="admin_logout"),
    path("admin/session-status/", admin_session_status, name="admin_session_status"),
    path("admin/", shopiva_admin_site.urls),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
