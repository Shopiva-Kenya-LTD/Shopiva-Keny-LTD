from django.conf import settings
from decimal import Decimal
import secrets
import string
import uuid
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from cloudinary.models import CloudinaryField


class SellerProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="seller_profile")
    business_name = models.CharField(max_length=200, blank=True)
    business_nature = models.CharField(max_length=500, blank=True, help_text="What products or services does the business sell or provide?")
    mpesa_phone = models.CharField(max_length=30, blank=True)
    business_address = models.CharField(max_length=255, blank=True, default="")
    business_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    business_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    commission_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=10,
        help_text="Shopiva platform commission. Sellers cannot set or change this rate.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        self.commission_percent = Decimal("10.00")
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.business_name or self.user.get_full_name() or self.user.username


class SellerWallet(models.Model):
    seller = models.OneToOneField(SellerProfile, on_delete=models.CASCADE, related_name="wallet")
    pending_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    available_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(pending_balance__gte=0), name="sellerwallet_pending_gte_0"),
            models.CheckConstraint(condition=models.Q(available_balance__gte=0), name="sellerwallet_available_gte_0"),
            models.CheckConstraint(condition=models.Q(total_sales__gte=0), name="sellerwallet_sales_gte_0"),
            models.CheckConstraint(condition=models.Q(total_commission__gte=0), name="sellerwallet_commission_gte_0"),
        ]

    def __str__(self):
        return f"{self.seller} wallet"


class Product(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    sku = models.CharField(max_length=64, unique=True, null=True, blank=True)
    stock_quantity = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    category = models.CharField(max_length=100, blank=True, default="General")
    brand = models.CharField(max_length=120, blank=True)
    gtin = models.CharField(max_length=32, blank=True)
    mpn = models.CharField(max_length=70, blank=True)
    image = CloudinaryField("image", folder="shopiva/products", blank=True, null=True)
    discount_percent = models.PositiveIntegerField(default=0)
    promo_text = models.CharField(max_length=120, blank=True)
    is_featured = models.BooleanField(default=False)
    media_ai_status = models.CharField(max_length=24, default="needs_review", choices=(("likely_real", "Likely real"), ("needs_review", "Needs review"), ("likely_ai", "Likely AI-generated")))
    media_ai_score = models.PositiveSmallIntegerField(default=50)
    media_ai_notes = models.CharField(max_length=500, blank=True)
    seller = models.ForeignKey(SellerProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="products")
    PACKAGE_MICRO = "micro"
    PACKAGE_SMALL = "small"
    PACKAGE_MEDIUM = "medium"
    PACKAGE_BIG = "big"
    PACKAGE_EXTRA_BIG = "extra_big"
    PACKAGE_CHOICES = (
        (PACKAGE_MICRO, "Micro"),
        (PACKAGE_SMALL, "Small"),
        (PACKAGE_MEDIUM, "Medium"),
        (PACKAGE_BIG, "Big"),
        (PACKAGE_EXTRA_BIG, "Extra Big"),
    )
    package_class = models.CharField(max_length=20, choices=PACKAGE_CHOICES, default=PACKAGE_SMALL)
    shipping_weight_kg = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    package_length_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    package_width_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    package_height_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    fulfillment_ready = models.BooleanField(default=True, help_text="Seller has supplied the shipping data required for platform fulfillment.")

    @property
    def discounted_price(self):
        discount = Decimal(self.discount_percent or 0)
        return self.price * (Decimal("100") - discount) / Decimal("100")

    @property
    def has_discount(self):
        return (self.discount_percent or 0) > 0

    def __str__(self):
        return self.name

    @property
    def fallback_image_url(self):
        """Stable visual fallback when a seller has not uploaded a real photo yet."""
        name = (self.name or "").lower()
        category = (self.category or "").lower()

        if any(token in name for token in ("iphone", "galaxy", "pixel", "tecno", "infinix", "oppo", "xiaomi", "oneplus", "vivo", "nokia", "phone", "tablet")):
            return "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("laptop", "computer", "office")):
            return "https://images.unsplash.com/photo-1496181133206-80ce9b88a853?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("tv", "entertainment", "gaming", "electronics")):
            return "https://images.unsplash.com/photo-1550009158-9ebf69173e03?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("fashion", "shoes")):
            return "https://images.unsplash.com/photo-1485230895905-ec40ba36b9bc?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("beauty", "skincare", "hair", "oral")):
            return "https://images.unsplash.com/photo-1596462502278-27bfdc403348?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("home", "furniture", "kitchen", "mattress")):
            return "https://images.unsplash.com/photo-1494438639946-1ebd1d20bf85?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("automotive", "vehicle", "motorcycle", "bicycle")):
            return "https://images.unsplash.com/photo-1486262715619-67b85e0b08d3?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("sports", "fitness")):
            return "https://images.unsplash.com/photo-1461896836934-ffe607ba8211?auto=format&fit=crop&w=1000&q=88"
        if any(token in category for token in ("agriculture", "garden", "farm")):
            return "https://images.unsplash.com/photo-1499529112087-3cb3b73cec95?auto=format&fit=crop&w=1000&q=88"
        return "https://images.unsplash.com/photo-1607082349566-187342175e2f?auto=format&fit=crop&w=1000&q=88"

    @property
    def has_real_image(self):
        try:
            return bool(self.image and self.image.url)
        except Exception:
            return False

    @property
    def average_rating(self):
        from django.db.models import Avg
        return self.reviews.aggregate(value=Avg("rating"))["value"] or 0

    @property
    def review_count(self):
        return self.reviews.count()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gt=0), name="product_price_gt_0"),
            models.CheckConstraint(condition=models.Q(discount_percent__gte=0, discount_percent__lte=100), name="product_discount_0_100"),
        ]

class ShopivaBranch(models.Model):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=40, unique=True)
    county = models.CharField(max_length=100)
    town = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    opening_time = models.TimeField(null=True, blank=True)
    closing_time = models.TimeField(null=True, blank=True)
    services = models.TextField(blank=True, help_text="Customer-facing services offered at this branch.")
    is_headquarters = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("county", "town", "name")

    def __str__(self):
        return f"{self.name} ({self.town}, {self.county})"


class ShopivaOutlet(models.Model):
    branch = models.ForeignKey(
        ShopivaBranch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outlets",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=40, unique=True)
    county = models.CharField(max_length=100)
    town = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    opening_time = models.TimeField(null=True, blank=True)
    closing_time = models.TimeField(null=True, blank=True)
    services = models.TextField(blank=True, help_text="Customer-facing services offered at this outlet.")
    pickup_available = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("county", "town", "name")

    def __str__(self):
        return f"{self.name} ({self.town}, {self.county})"


class DeliveryHub(models.Model):
    FULFILLMENT_OWN = "shopiva_owned"
    FULFILLMENT_THIRD_PARTY = "third_party"
    FULFILLMENT_INDEPENDENT = "independent"
    FULFILLMENT_MIXED = "mixed"
    FULFILLMENT_CHOICES = (
        (FULFILLMENT_OWN, "Shopiva-owned fleet"),
        (FULFILLMENT_THIRD_PARTY, "Third-party couriers"),
        (FULFILLMENT_INDEPENDENT, "Independent riders/drivers"),
        (FULFILLMENT_MIXED, "Mixed fulfillment"),
    )

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=30, unique=True)
    county = models.CharField(max_length=100)
    town = models.CharField(max_length=120)
    address = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    fulfillment_model = models.CharField(max_length=30, choices=FULFILLMENT_CHOICES, default=FULFILLMENT_MIXED)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-is_primary", "name")
        constraints = [
            models.CheckConstraint(condition=models.Q(latitude__gte=-90, latitude__lte=90), name="deliveryhub_latitude_range"),
            models.CheckConstraint(condition=models.Q(longitude__gte=-180, longitude__lte=180), name="deliveryhub_longitude_range"),
        ]

    def __str__(self):
        return f"{self.name} ({self.town}, {self.county})"


class DeliveryPickupPoint(models.Model):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=40, unique=True)
    county = models.CharField(max_length=100)
    town = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    phone = models.CharField(max_length=30, blank=True)
    partner_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    max_holding_days = models.PositiveSmallIntegerField(default=7)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("county", "town", "name")

    def __str__(self):
        return f"{self.name} ({self.town}, {self.county})"


class DeliveryRateCard(models.Model):
    ROUTE_LOCAL = "local"
    ROUTE_REGIONAL = "regional"
    ROUTE_NATIONAL = "national"
    ROUTE_REMOTE = "remote"
    ROUTE_CHOICES = (
        (ROUTE_LOCAL, "Local"),
        (ROUTE_REGIONAL, "Regional"),
        (ROUTE_NATIONAL, "National"),
        (ROUTE_REMOTE, "Remote / Rural"),
    )

    name = models.CharField(max_length=150)
    fulfillment_model = models.CharField(max_length=30, choices=DeliveryHub.FULFILLMENT_CHOICES, default=DeliveryHub.FULFILLMENT_MIXED)
    delivery_mode = models.CharField(max_length=20, choices=(("standard", "Standard Delivery"), ("pickup", "Pickup Station"), ("express", "Express Delivery")), default="standard")
    package_class = models.CharField(max_length=20, choices=Product.PACKAGE_CHOICES, default=Product.PACKAGE_SMALL)
    route_class = models.CharField(max_length=20, choices=ROUTE_CHOICES, default=ROUTE_LOCAL)
    base_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    per_km_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    per_extra_seller_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    minimum_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    maximum_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    rounding_step = models.DecimalField(max_digits=10, decimal_places=2, default=10)
    is_active = models.BooleanField(default=False)
    notes = models.TextField(blank=True, help_text="Document the approved courier/platform rate basis before activation.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("delivery_mode", "package_class", "route_class", "name")

    def __str__(self):
        return f"{self.name} — {self.get_package_class_display()} / {self.get_route_class_display()}"


class DeliveryPricingProfile(models.Model):
    MODE_DISTANCE = "distance"
    MODE_LEGACY = "legacy_tariff"
    PRICING_MODE_CHOICES = (
        (MODE_DISTANCE, "Distance-based pricing"),
        (MODE_LEGACY, "Destination tariff pricing"),
    )

    name = models.CharField(max_length=120)
    delivery_mode = models.CharField(
        max_length=20,
        choices=(
            ("standard", "Standard Delivery"),
            ("pickup", "Pickup Station"),
            ("express", "Express Delivery"),
        ),
        default="standard",
    )
    pricing_mode = models.CharField(max_length=30, choices=PRICING_MODE_CHOICES, default=MODE_DISTANCE)
    base_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    per_km_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    per_seller_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rural_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    minimum_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    maximum_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    rounding_step = models.DecimalField(max_digits=10, decimal_places=2, default=10)
    is_active = models.BooleanField(default=False)
    notes = models.TextField(blank=True, help_text="Record the approved commercial/transport basis for these rates.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("delivery_mode", "name")
        constraints = [
            models.CheckConstraint(condition=models.Q(base_fee__gte=0), name="deliverypricing_base_gte_0"),
            models.CheckConstraint(condition=models.Q(per_km_fee__gte=0), name="deliverypricing_km_gte_0"),
            models.CheckConstraint(condition=models.Q(per_seller_fee__gte=0), name="deliverypricing_seller_gte_0"),
            models.CheckConstraint(condition=models.Q(rural_surcharge__gte=0), name="deliverypricing_rural_gte_0"),
            models.CheckConstraint(condition=models.Q(minimum_fee__gte=0), name="deliverypricing_min_gte_0"),
            models.CheckConstraint(condition=models.Q(maximum_fee__gte=0) | models.Q(maximum_fee__isnull=True), name="deliverypricing_max_gte_0"),
            models.CheckConstraint(condition=models.Q(rounding_step__gt=0), name="deliverypricing_rounding_gt_0"),
        ]

    def __str__(self):
        return f"{self.name} — {self.get_delivery_mode_display()}"


class DeliveryTariff(models.Model):
    MODE_STANDARD = "standard"
    MODE_PICKUP = "pickup"
    MODE_EXPRESS = "express"
    MODE_CHOICES = (
        (MODE_STANDARD, "Standard Delivery"),
        (MODE_PICKUP, "Pickup Station"),
        (MODE_EXPRESS, "Express Delivery"),
    )

    county = models.CharField(max_length=100)
    destination = models.CharField(max_length=120)
    standard_fee = models.DecimalField(max_digits=10, decimal_places=2)
    pickup_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    express_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    pickup_available = models.BooleanField(default=False)
    express_available = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_fallback = models.BooleanField(default=False, help_text="Use this tariff for any other delivery point in the county when no exact destination tariff exists.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("county", "destination")
        constraints = [
            models.UniqueConstraint(fields=("county", "destination"), name="unique_shopiva_delivery_tariff_destination"),
            models.CheckConstraint(condition=models.Q(standard_fee__gte=0), name="deliverytariff_standard_fee_gte_0"),
            models.CheckConstraint(condition=models.Q(pickup_fee__gte=0) | models.Q(pickup_fee__isnull=True), name="deliverytariff_pickup_fee_gte_0"),
            models.CheckConstraint(condition=models.Q(express_fee__gte=0) | models.Q(express_fee__isnull=True), name="deliverytariff_express_fee_gte_0"),
        ]

    def fee_for_mode(self, mode=MODE_STANDARD):
        if mode == self.MODE_PICKUP:
            if not self.pickup_available or self.pickup_fee is None:
                raise ValueError("Pickup Station delivery is not currently available for this destination.")
            return self.pickup_fee
        if mode == self.MODE_EXPRESS:
            if not self.express_available or self.express_fee is None:
                raise ValueError("Express delivery is not currently available for this destination.")
            return self.express_fee
        return self.standard_fee

    def __str__(self):
        return f"{self.destination}, {self.county} — KSh {self.standard_fee}"


class DeliveryAgent(models.Model):
    STATUS_CHOICES = [("offline", "Offline"), ("available", "Available"), ("on_delivery", "On Delivery")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="delivery_agent_profile")
    phone = models.CharField(max_length=30, blank=True)
    vehicle_type = models.CharField(max_length=80, blank=True)
    vehicle_number = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="offline")
    current_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    current_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    last_location_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def display_name(self):
        return self.user.get_full_name() or self.user.username

    @property
    def location_is_live(self):
        from django.utils import timezone
        if not self.last_location_at:
            return False
        return (timezone.now() - self.last_location_at).total_seconds() <= 90

    def __str__(self):
        return self.display_name


class DeliveryPayProfile(models.Model):
    name = models.CharField(max_length=120, unique=True)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    minimum_payout = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("100.00"))
    auto_payout_enabled = models.BooleanField(default=False)
    auto_payout_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("100.00"))
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-is_active", "-updated_at")
        constraints = [
            models.CheckConstraint(condition=models.Q(commission_percent__gte=0, commission_percent__lte=100), name="deliverypay_commission_0_100"),
            models.CheckConstraint(condition=models.Q(minimum_payout__gt=0), name="deliverypay_min_payout_gt_0"),
            models.CheckConstraint(condition=models.Q(auto_payout_threshold__gt=0), name="deliverypay_auto_threshold_gt_0"),
        ]

    def __str__(self):
        return self.name


class DeliveryWallet(models.Model):
    agent = models.OneToOneField(DeliveryAgent, on_delete=models.CASCADE, related_name="wallet")
    payout_phone = models.CharField(max_length=30, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    bank_code = models.CharField(max_length=20, blank=True)
    bank_account_name = models.CharField(max_length=160, blank=True)
    bank_account_number = models.CharField(max_length=40, blank=True)
    available_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    pending_payout_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_earned = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    auto_payout_enabled = models.BooleanField(default=False)
    auto_payout_threshold = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("100.00"))
    last_payout_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(available_balance__gte=0), name="deliverywallet_available_gte_0"),
            models.CheckConstraint(condition=models.Q(pending_payout_balance__gte=0), name="deliverywallet_pending_gte_0"),
            models.CheckConstraint(condition=models.Q(total_earned__gte=0), name="deliverywallet_earned_gte_0"),
            models.CheckConstraint(condition=models.Q(total_paid__gte=0), name="deliverywallet_paid_gte_0"),
            models.CheckConstraint(condition=models.Q(auto_payout_threshold__gt=0), name="deliverywallet_auto_threshold_gt_0"),
        ]

    def __str__(self):
        return f"{self.agent} wallet"


class DeliveryPayout(models.Model):
    STATUS_REQUESTED = "requested"
    STATUS_PROCESSING = "processing"
    STATUS_PAID = "paid"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_REQUESTED, "Awaiting admin payment"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_PAID, "Paid"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )
    TRIGGER_MANUAL = "manual"
    TRIGGER_ADMIN = "admin"
    TRIGGER_CHOICES = (
        (TRIGGER_MANUAL, "Rider requested"),
        (TRIGGER_ADMIN, "Admin"),
    )

    agent = models.ForeignKey(DeliveryAgent, on_delete=models.PROTECT, related_name="payouts")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default=TRIGGER_MANUAL)
    provider = models.CharField(max_length=30, default="admin_mpesa")
    bank_name = models.CharField(max_length=120, blank=True)
    bank_code = models.CharField(max_length=20, blank=True)
    bank_account_name = models.CharField(max_length=160, blank=True)
    bank_account_number = models.CharField(max_length=40, blank=True)
    provider_reference = models.CharField(max_length=120, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="deliverypayout_amount_gt_0"),
        ]
        indexes = [
            models.Index(fields=("agent", "status", "created_at")),
        ]

    def __str__(self):
        return f"Delivery payout #{self.id} — {self.agent} — KSh {self.amount}"


class DeliveryEarning(models.Model):
    STATUS_AVAILABLE = "available"
    STATUS_RESERVED = "reserved"
    STATUS_PAID = "paid"
    STATUS_REVERSED = "reversed"
    STATUS_CHOICES = (
        (STATUS_AVAILABLE, "Available"),
        (STATUS_RESERVED, "Reserved for payout"),
        (STATUS_PAID, "Paid"),
        (STATUS_REVERSED, "Reversed"),
    )

    order = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="delivery_earning")
    agent = models.ForeignKey(DeliveryAgent, on_delete=models.PROTECT, related_name="earnings")
    pay_profile = models.ForeignKey(DeliveryPayProfile, on_delete=models.PROTECT, related_name="earnings")
    distance_km = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    distance_source = models.CharField(max_length=30, default="estimated")
    base_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    distance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_AVAILABLE)
    payout = models.ForeignKey(DeliveryPayout, on_delete=models.SET_NULL, null=True, blank=True, related_name="earnings")
    earned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-earned_at",)
        constraints = [
            models.CheckConstraint(condition=models.Q(distance_km__gte=0), name="deliveryearning_distance_gte_0"),
            models.CheckConstraint(condition=models.Q(base_amount__gte=0), name="deliveryearning_base_gte_0"),
            models.CheckConstraint(condition=models.Q(distance_amount__gte=0), name="deliveryearning_distance_amt_gte_0"),
            models.CheckConstraint(condition=models.Q(total_amount__gt=0), name="deliveryearning_total_gt_0"),
        ]
        indexes = [
            models.Index(fields=("agent", "status", "earned_at")),
        ]

    def __str__(self):
        return f"Earning #{self.id} — Order #{self.order_id} — KSh {self.total_amount}"


class Order(models.Model):
    STATUS_CHOICES = [("pending", "Placed / Pending"), ("confirmed", "Confirmed"), ("paid", "Paid"), ("packed", "Packed"), ("processing", "Processing"), ("shipped", "Shipped"), ("out_for_delivery", "Out for Delivery"), ("delivered", "Delivered"), ("cancelled", "Cancelled")]
    PAYMENT_STATUS_CHOICES = [("unpaid", "Unpaid"), ("pending", "Payment Pending"), ("paid", "Paid"), ("failed", "Failed"), ("refunded", "Refunded")]
    customer_name = models.CharField(max_length=200)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="shopiva_orders")
    email = models.EmailField()
    access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    phone = models.CharField(max_length=30)
    address = models.TextField()
    delivery_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    delivery_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    items_subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    platform_commission_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_county = models.CharField(max_length=100, blank=True, default="")
    delivery_town = models.CharField(max_length=120, blank=True, default="")
    delivery_mode = models.CharField(max_length=20, default="standard")
    delivery_distance_km = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_distance_source = models.CharField(max_length=30, default="estimated")
    delivery_hub = models.ForeignKey("DeliveryHub", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    delivery_pricing_profile = models.ForeignKey("DeliveryPricingProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    delivery_pricing_basis = models.CharField(max_length=40, default="legacy_tariff")
    delivery_base_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_distance_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_distance_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    delivery_package_class = models.CharField(max_length=20, default="small")
    delivery_route_class = models.CharField(max_length=20, default="national")
    delivery_rate_card = models.ForeignKey("DeliveryRateCard", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    delivery_pickup_point = models.ForeignKey("DeliveryPickupPoint", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="pending")
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default="unpaid")
    payment_reference = models.CharField(max_length=120, blank=True)
    tracking_code = models.CharField(max_length=40, unique=True, null=True, blank=True)
    delivery_agent = models.ForeignKey(DeliveryAgent, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    packed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    delivery_confirmation_code = models.CharField(max_length=6, blank=True, editable=False)
    delivery_verification_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    delivery_verification_locked_at = models.DateTimeField(null=True, blank=True, editable=False)
    customer_delivery_confirmed = models.BooleanField(default=False)
    customer_delivery_confirmed_at = models.DateTimeField(null=True, blank=True)
    customer_delivery_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_shopiva_deliveries",
    )
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def ensure_delivery_confirmation_code(self):
        if not self.delivery_confirmation_code:
            self.delivery_confirmation_code = "".join(secrets.choice(string.digits) for _ in range(6))
            self.delivery_verification_attempts = 0
            self.delivery_verification_locked_at = None
        return self.delivery_confirmation_code

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(total_amount__gte=0), name="order_total_gte_0"),
        ]

    def __str__(self):
        return f"Order #{self.id} - {self.customer_name}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    seller = models.ForeignKey(SellerProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_items")
    seller_gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    platform_commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    seller_net = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="orderitem_quantity_gt_0"),
            models.CheckConstraint(condition=models.Q(price__gte=0), name="orderitem_price_gte_0"),
            models.CheckConstraint(condition=models.Q(seller_gross__gte=0), name="orderitem_gross_gte_0"),
            models.CheckConstraint(condition=models.Q(platform_commission__gte=0), name="orderitem_commission_gte_0"),
            models.CheckConstraint(condition=models.Q(seller_net__gte=0), name="orderitem_net_gte_0"),
        ]

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class OrderEvent(models.Model):
    EVENT_CHOICES = [("placed", "Order Placed"), ("confirmed", "Order Confirmed"), ("payment_pending", "Payment Pending"), ("paid", "Payment Confirmed"), ("packed", "Order Packed"), ("assigned", "Delivery Agent Assigned"), ("processing", "Processing"), ("shipped", "Shipped"), ("out_for_delivery", "Out for Delivery"), ("delivered", "Delivered"), ("cancelled", "Order Cancelled")]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=30, choices=EVENT_CHOICES)
    note = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="shopiva_order_events")
    delivery_agent = models.ForeignKey(DeliveryAgent, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_events")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Order #{self.order_id} - {self.get_event_type_display()}"


class DeliveryLocationPing(models.Model):
    agent = models.ForeignKey(DeliveryAgent, on_delete=models.CASCADE, related_name="location_history")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    accuracy_meters = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    speed_mps = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    heading_degrees = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-recorded_at",)
        indexes = [models.Index(fields=("agent", "-recorded_at"))]
        constraints = [
            models.CheckConstraint(condition=models.Q(latitude__gte=-90, latitude__lte=90), name="deliveryping_latitude_range"),
            models.CheckConstraint(condition=models.Q(longitude__gte=-180, longitude__lte=180), name="deliveryping_longitude_range"),
            models.CheckConstraint(condition=models.Q(accuracy_meters__gte=0) | models.Q(accuracy_meters__isnull=True), name="deliveryping_accuracy_gte_0"),
            models.CheckConstraint(condition=models.Q(speed_mps__gte=0) | models.Q(speed_mps__isnull=True), name="deliveryping_speed_gte_0"),
        ]

    def __str__(self):
        return f"{self.agent.display_name} @ {self.recorded_at:%Y-%m-%d %H:%M:%S}"


class PaymentTransaction(models.Model):
    METHOD_CHOICES = [("mpesa", "M-PESA"), ("card", "Card"), ("cod", "Cash on Delivery")]
    STATUS_CHOICES = [("initiated", "Initiated"), ("pending", "Pending"), ("paid", "Paid"), ("failed", "Failed"), ("cancelled", "Cancelled"), ("refunded", "Refunded")]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="initiated")
    provider = models.CharField(max_length=40, default="shopiva")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone = models.CharField(max_length=30, blank=True)
    merchant_request_id = models.CharField(max_length=120, blank=True, db_index=True)
    checkout_request_id = models.CharField(max_length=120, blank=True, db_index=True)
    provider_reference = models.CharField(max_length=120, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=120, unique=True)
    raw_response = models.JSONField(default=dict, blank=True)
    inventory_released = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("order", "status"))]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_gt_0"),
        ]

    def __str__(self):
        return f"{self.method.upper()} #{self.id} - Order #{self.order_id}"


class CustomerAddress(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shopiva_addresses")
    label = models.CharField(max_length=80, default="Home")
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=30)
    county = models.CharField(max_length=100)
    town = models.CharField(max_length=100)
    address_line = models.CharField(max_length=255)
    landmark = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-is_default", "-created_at")
        constraints = [
            models.UniqueConstraint(fields=("user",), condition=models.Q(is_default=True), name="unique_default_shopiva_address"),
        ]

    def __str__(self):
        return f"{self.label} - {self.town}, {self.county}"


class WishlistItem(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shopiva_wishlist")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="wishlist_items")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=("user", "product"), name="unique_shopiva_wishlist_item")]

    def __str__(self):
        return f"{self.user.username} - {self.product.name}"


class SellerPayoutRequest(models.Model):
    STATUS_CHOICES = [
        ("requested", "Requested"),
        ("processing", "Processing"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]
    seller = models.ForeignKey(SellerProfile, on_delete=models.PROTECT, related_name="payout_requests")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone = models.CharField(max_length=30)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="requested")
    provider_reference = models.CharField(max_length=120, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="sellerpayout_amount_gt_0"),
        ]

    def __str__(self):
        return f"Payout #{self.id} - {self.seller} - KSh {self.amount}"


class SellerSettlement(models.Model):
    STATUS_CHOICES = [("pending", "Pending delivery"), ("available", "Available for payout"), ("paid", "Paid to seller"), ("held", "Held"), ("refunded", "Refunded")]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="seller_settlements")
    seller = models.ForeignKey(SellerProfile, on_delete=models.PROTECT, related_name="settlements")
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    platform_commission = models.DecimalField(max_digits=12, decimal_places=2)
    seller_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    provider_reference = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("order", "seller"), name="unique_order_seller_settlement"),
            models.CheckConstraint(condition=models.Q(gross_amount__gte=0), name="settlement_gross_gte_0"),
            models.CheckConstraint(condition=models.Q(platform_commission__gte=0), name="settlement_commission_gte_0"),
            models.CheckConstraint(condition=models.Q(seller_amount__gte=0), name="settlement_seller_gte_0"),
        ]

    def __str__(self):
        return f"Settlement #{self.id} - Order #{self.order_id}"


class ProductReview(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shopiva_reviews")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="product_reviews")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("product", "customer", "order"), name="unique_product_review_per_order"),
            models.CheckConstraint(condition=models.Q(rating__gte=1, rating__lte=5), name="productreview_rating_1_5"),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.rating}/5"


class Notification(models.Model):
    TYPE_CHOICES = [("order", "Order"), ("payment", "Payment"), ("delivery", "Delivery"), ("payout", "Payout"), ("system", "System")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shopiva_notifications")
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="system")
    title = models.CharField(max_length=160)
    message = models.TextField()
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user.username}: {self.title}"


class NotificationDelivery(models.Model):
    CHANNEL_CHOICES = (
        ("email", "Email"),
        ("sms", "SMS"),
        ("whatsapp", "WhatsApp"),
    )
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("accepted", "Accepted by provider"),
        ("delivered", "Delivered"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),
    )
    notification = models.ForeignKey("Notification", on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    provider_message_id = models.CharField(max_length=160, blank=True)
    provider_status = models.CharField(max_length=120, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=("notification", "channel"), name="unique_notification_delivery_channel")]

    def __str__(self):
        return f"{self.notification_id} · {self.channel} · {self.status}"


class StaffPayment(models.Model):
    METHOD_MPESA = "mpesa"
    METHOD_BANK = "bank"
    METHOD_CASH = "cash"
    METHOD_OTHER = "other"
    METHOD_CHOICES = (
        (METHOD_MPESA, "M-PESA"),
        (METHOD_BANK, "Bank transfer"),
        (METHOD_CASH, "Cash"),
        (METHOD_OTHER, "Other"),
    )

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_PAID = "paid"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending payment"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_PAID, "Paid"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="staff_payments_received",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_MPESA)
    destination = models.CharField(
        max_length=120,
        blank=True,
        help_text="M-PESA number, bank account/reference, or other payment destination.",
    )
    purpose = models.CharField(max_length=160)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    provider_reference = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="staff_payments_created",
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_payments_paid",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=120, unique=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="staffpayment_amount_gt_0"),
        ]

    def __str__(self):
        return f"Staff payment #{self.id} - {self.recipient} - KSh {self.amount}"


class NiaCallSession(models.Model):
    ROLE_CUSTOMER = "customer"
    ROLE_SELLER = "seller"
    ROLE_ADMIN = "admin"
    ROLE_CHOICES = (
        (ROLE_CUSTOMER, "Customer"),
        (ROLE_SELLER, "Seller"),
        (ROLE_ADMIN, "Admin"),
    )

    STATUS_QUEUED = "queued"
    STATUS_RINGING = "ringing"
    STATUS_IN_PROGRESS = "in-progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    STATUS_NO_ANSWER = "no-answer"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "Queued"),
        (STATUS_RINGING, "Ringing"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELED, "Canceled"),
        (STATUS_NO_ANSWER, "No answer"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    DIRECTION_OUTBOUND = "outbound"
    DIRECTION_INBOUND = "inbound"
    DIRECTION_CHOICES = (
        (DIRECTION_OUTBOUND, "Outbound"),
        (DIRECTION_INBOUND, "Inbound"),
    )
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default=DIRECTION_OUTBOUND)
    caller_verified = models.BooleanField(default=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nia_calls",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    phone_e164 = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    provider = models.CharField(max_length=30, default="twilio")
    provider_sid = models.CharField(max_length=100, blank=True, db_index=True)
    conversation = models.JSONField(default=list, blank=True)
    last_user_text = models.TextField(blank=True)
    last_ai_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    verification = models.ForeignKey(
        "NiaCallerVerification",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="call_sessions",
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Nia call {self.id} · {self.role} · {self.status}"


class NiaCallerVerification(models.Model):
    ROLE_CHOICES = NiaCallSession.ROLE_CHOICES

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nia_caller_verifications",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    phone_e164 = models.CharField(max_length=20, db_index=True)
    pin_code = models.CharField(max_length=128)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("phone_e164", "expires_at")),
        ]

    def __str__(self):
        return f"Nia caller verification · {self.role} · {self.phone_e164}"


class NiaAuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="nia_audit_logs",
    )
    role = models.CharField(max_length=20, choices=NiaCallSession.ROLE_CHOICES)
    action = models.CharField(max_length=100)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "created_at")),
            models.Index(fields=("role", "action", "created_at")),
        ]

    def __str__(self):
        return f"Nia audit · {self.role} · {self.action}"


class NiaTask(models.Model):
    TRIGGER_MANUAL = "manual"
    TRIGGER_ORDER = "order"
    TRIGGER_PAYMENT = "payment"
    TRIGGER_DELIVERY = "delivery"
    TRIGGER_LOW_STOCK = "low_stock"
    TRIGGER_SCHEDULE = "schedule"
    TRIGGER_CHOICES = (
        (TRIGGER_MANUAL, "Manual"),
        (TRIGGER_ORDER, "Order"),
        (TRIGGER_PAYMENT, "Payment"),
        (TRIGGER_DELIVERY, "Delivery"),
        (TRIGGER_LOW_STOCK, "Low stock"),
        (TRIGGER_SCHEDULE, "Scheduled"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="nia_tasks",
    )
    role = models.CharField(max_length=20, choices=NiaCallSession.ROLE_CHOICES)
    title = models.CharField(max_length=180)
    instruction = models.TextField()
    trigger_kind = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default=TRIGGER_MANUAL)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    enabled = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("next_run_at", "-created_at")

    def __str__(self):
        return f"{self.title} · {self.user_id}"
