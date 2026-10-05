from django.apps import AppConfig


class HomeConfig(AppConfig):
    name = "home"

    def ready(self):
        # Register product-media and order-notification signals.
        from . import media_signals  # noqa: F401
        from . import order_notifications  # noqa: F401
        from . import delivery_signals  # noqa: F401
