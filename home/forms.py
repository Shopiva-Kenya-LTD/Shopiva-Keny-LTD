from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.text import slugify
from django.utils.html import conditional_escape, mark_safe
from django.core.exceptions import ValidationError
from django.urls import reverse
import uuid
import re

from .models import Product, ProductReview
from .media_pipeline import enhance_product_image, upload_product_image
from .shopiva_seller_catalog import (
    catalog_search_choices as base_catalog_search_choices,
    resolve_catalog_item as base_resolve_catalog_item,
)
from .shopiva_catalog_2026_expansion import catalog_choices_2026, catalog_item_2026
from .shopiva_catalog_complete import catalog_choices_complete, catalog_item_complete


def catalog_search_choices():
    """Return one deduplicated master catalogue covering the full seller marketplace."""
    sources = (
        base_catalog_search_choices(limit=10000),
        catalog_choices_2026(),
        catalog_choices_complete(),
    )
    seen = set()
    choices = []
    for source in sources:
        for key, label in source:
            normalized = label.casefold().strip()
            if normalized in seen:
                continue
            seen.add(normalized)
            choices.append((key, label))
    return tuple(choices)


def resolve_catalog_item(key):
    """Resolve a catalogue key, visible datalist label, or product search phrase."""
    direct = (
        catalog_item_complete(key)
        or catalog_item_2026(key)
        or base_resolve_catalog_item(key)
    )
    if direct:
        return direct

    text = str(key or "").strip().casefold()
    if not text:
        return None

    sources = (
        base_catalog_search_choices(limit=10000),
        catalog_choices_2026(),
        catalog_choices_complete(),
    )
    for source in sources:
        for item_key, label in source:
            if str(label).strip().casefold() == text:
                return (
                    catalog_item_complete(item_key)
                    or catalog_item_2026(item_key)
                    or base_resolve_catalog_item(item_key)
                )
    return None


def catalog_browser_choices():
    """Return structured catalogue rows for the seller dashboard browser."""
    rows = []
    for key, label in catalog_search_choices():
        if " → " in label:
            category, brand, name = label.split(" → ", 2)
        elif " — " in label:
            name, category, brand = label.rsplit(" — ", 2)
        else:
            name, category, brand = label, "General", "Universal"
        rows.append({
            "key": key,
            "name": name,
            "category": category,
            "brand": brand,
            "label": label,
        })
    return rows


def _validate_unique_username(username, *, error_message):
    """Normalize and enforce Shopiva's case-insensitive username rule in one place."""
    normalized = str(username or "").strip()
    if normalized and User.objects.filter(username__iexact=normalized).exists():
        raise forms.ValidationError(error_message)
    return normalized


class _ShopivaUsernameBoundary:
    """Single deterministic username validation boundary for registration forms."""

    username_error_message = "Username exists. Please choose a different username."

    def clean_username(self):
        username = self.cleaned_data.get("username", "")
        return _validate_unique_username(username, error_message=self.username_error_message)

    def validate_unique(self):
        """Prevent Django's second model-level username check from replacing our message."""
        return None


class CustomerRegistrationForm(_ShopivaUsernameBoundary, UserCreationForm):
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"placeholder": "you@example.com", "autocomplete": "email"}),
    )

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "This email is already registered. Please use a different email or sign in."
            )
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["username"].strip()
        user.email = self.cleaned_data["email"].strip().lower()
        if commit:
            user.save()
        return user


class DeliveryRegistrationForm(_ShopivaUsernameBoundary, UserCreationForm):
    """Create a delivery-partner application; access remains disabled until Shopiva approves it."""

    username_error_message = "Username exists. Please choose another username."

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "you@example.com"}),
    )
    first_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={"autocomplete": "given-name", "placeholder": "First name"}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={"autocomplete": "family-name", "placeholder": "Last name"}),
    )
    phone = forms.CharField(
        max_length=30,
        required=True,
        widget=forms.TextInput(attrs={"autocomplete": "tel", "placeholder": "07XX XXX XXX"}),
        help_text="Kenyan mobile number used by Shopiva operations.",
    )
    vehicle_type = forms.CharField(
        max_length=80,
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "Motorbike, car, bicycle, walking, etc."}),
    )
    vehicle_number = forms.CharField(
        max_length=40,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Optional plate / registration number"}),
    )

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email is already registered.")
        return email

    def clean_phone(self):
        raw = self.cleaned_data.get("phone", "").strip()
        digits = re.sub(r"\D", "", raw)
        if digits.startswith("00"):
            digits = digits[2:]
        if digits.startswith("0") and len(digits) == 10:
            digits = "254" + digits[1:]
        elif digits.startswith("7") and len(digits) == 9:
            digits = "254" + digits
        if not re.fullmatch(r"254(7|1)\d{8}", digits):
            raise forms.ValidationError("Enter a valid Kenyan mobile number, e.g. 0712345678.")
        from .models import DeliveryAgent
        if DeliveryAgent.objects.filter(phone=digits).exists():
            raise forms.ValidationError("This phone number is already linked to a Shopiva delivery account.")
        return digits

    def clean_vehicle_number(self):
        return self.cleaned_data.get("vehicle_number", "").strip().upper()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"].strip().lower()
        user.first_name = self.cleaned_data["first_name"].strip()
        user.last_name = self.cleaned_data["last_name"].strip()
        user.is_staff = False
        user.is_superuser = False
        # Keep the Django login account usable while the DeliveryAgent profile remains
        # pending. Delivery access itself is still blocked until admin approval,
        # or the no-active-admin fallback in delivery_login() is triggered.
        user.is_active = True
        if commit:
            from .models import DeliveryAgent
            user.save()
            agent = DeliveryAgent.objects.create(
                user=user,
                phone=self.cleaned_data["phone"],
                vehicle_type=self.cleaned_data["vehicle_type"].strip(),
                vehicle_number=self.cleaned_data["vehicle_number"],
                status="offline",
                is_active=False,
            )
            # Surface the application in every active admin's notification center.
            # Use the same notification service as the rest of Shopiva so the alert
            # is stored in-app and can also use configured email/SMS/WhatsApp channels.
            approval_url = reverse("shopiva_admin:approval_center")
            admins = list(User.objects.filter(is_staff=True, is_active=True).only("id", "email"))
            from .notification_service import notify_user
            for admin in admins:
                notify_user(
                    admin,
                    "system",
                    "New staff approval required",
                    (
                        f"{user.get_full_name() or user.username} submitted a delivery staff application. "
                        "Verify the applicant's identity, phone and vehicle details before approving access."
                    ),
                    link=approval_url,
                )
        return user


class SellerRegistrationForm(_ShopivaUsernameBoundary, UserCreationForm):
    username_error_message = "Username exists. Please choose another username."
    email = forms.EmailField(required=True)
    business_name = forms.CharField(max_length=200)
    business_nature = forms.CharField(
        max_length=500,
        required=True,
        label="Nature of business",
        help_text="Briefly describe what your business sells or provides.",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. Hardware, building materials and tools supplied to contractors and homeowners."}),
    )
    mpesa_phone = forms.CharField(
        max_length=30,
        help_text="Kenyan M-PESA number for future seller payouts.",
    )

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("This email is already registered.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"].strip().lower()
        user.username = self.cleaned_data["username"].strip()
        if commit:
            user.save()
        return user


class MultipleImageInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    widget = MultipleImageInput

    def clean(self, data, initial=None):
        if not data:
            return []
        if isinstance(data, (list, tuple)):
            return [super().clean(item, initial=None) for item in data]
        return [super().clean(data, initial=initial)]


class CatalogSearchWidget(forms.TextInput):
    """Search-first seller catalogue field with browser-native suggestions."""

    input_type = "search"

    def __init__(self, attrs=None):
        base = {
            "placeholder": "Search phones, accessories, appliances, utensils, car/motorcycle/bicycle spares and more...",
            "autocomplete": "off",
            "aria-label": "Search and choose a Shopiva product",
        }
        if attrs:
            base.update(attrs)
        super().__init__(attrs=base)

    def render(self, name, value, attrs=None, renderer=None):
        rendered = super().render(name, value, attrs, renderer)
        options = []
        for key, label in catalog_search_choices():
            options.append(
                f'<option value="{conditional_escape(label)}" data-key="{conditional_escape(key)}"></option>'
            )
        datalist = (
            '<datalist id="shopiva-product-catalog-options">'
            + "".join(options)
            + '<option value="CUSTOM PRODUCT — enter your own product"></option>'
            + "</datalist>"
        )
        return mark_safe(rendered + datalist)


class SellerProductForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        kwargs.pop("seller", None)
        super().__init__(*args, **kwargs)
        # A listing must remain publishable even if Cloudinary is temporarily unavailable.
        # Real photos are preferred and uploaded when Cloudinary is healthy; the marketplace
        # keeps a stable visual fallback when no photo is available.
        self.fields["image"].required = False
        self.fields["image"].help_text = (
            "Optional but recommended. Upload a clear real photo of the exact product; "
            "Shopiva will enhance it and store it in Cloudinary when available."
        )

    catalog_product = forms.CharField(
        required=False,
        label="Search & choose from Shopiva master catalogue",
        help_text=(
            "Type a product name, brand, model or spare part. Shopiva searches a broad marketplace "
            "catalogue covering phones, computers, electronics, home, furniture, fashion, groceries, building, "
            "electronics, utensils, appliances, car parts, motorcycle parts, bicycle parts/customisation, "
            "tools, agriculture, automotive, motorcycles, bicycles, office, beauty, sports, baby, pets and many everyday categories. Choosing a catalogue suggestion fills the product "
            "name and category automatically. Type CUSTOM PRODUCT to list something new."
        ),
        widget=CatalogSearchWidget(attrs={"list": "shopiva-product-catalog-options", "class": "shopiva-catalog-search"}),
    )
    discount_percent = forms.IntegerField(
        min_value=0,
        max_value=100,
        required=False,
        help_text=(
            "Optional customer discount from the original price. Example: 20 means the customer pays 80% of the listed price."
        ),
    )
    promo_text = forms.CharField(
        max_length=120,
        required=False,
        help_text="Optional short marketing message shown with the product, e.g. 'Free delivery' or 'Weekend Deal'.",
    )
    gallery_images = MultipleImageField(
        required=False,
        label="Additional product photos (up to 8)",
        help_text="Use real photos of the same product. Shopiva automatically enhances them for the marketplace gallery.",
    )

    class Meta:
        model = Product
        fields = (
            "catalog_product",
            "name",
            "description",
            "category",
            "brand",
            "gtin",
            "mpn",
            "price",
            "stock_quantity",
            "discount_percent",
            "promo_text",
            "image",
            "gallery_images",
            "is_active",
            "is_featured",
        )
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Catalogue selection will fill this, or enter a custom product"}),
            "description": forms.Textarea(attrs={"rows": 5}),
            "category": forms.TextInput(attrs={"placeholder": "Electronics, Fashion, Groceries, Vehicle Parts..."}),
            "brand": forms.TextInput(attrs={"placeholder": "Manufacturer/brand, if printed on the product"}),
            "gtin": forms.TextInput(attrs={"placeholder": "GTIN/barcode (leave blank if none)"}),
            "mpn": forms.TextInput(attrs={"placeholder": "Manufacturer part number (leave blank if none)"}),
            "image": forms.ClearableFileInput(attrs={"accept": "image/*"}),
        }

    def clean_catalog_product(self):
        raw = self.cleaned_data.get("catalog_product", "").strip()
        if not raw or raw.upper().startswith("CUSTOM PRODUCT"):
            return ""
        item = resolve_catalog_item(raw)
        if not item:
            raise forms.ValidationError(
                "No catalogue product matched that search. Choose a suggestion or type CUSTOM PRODUCT to enter your own item."
            )
        return item["key"]

    def clean(self):
        cleaned = super().clean()
        item = resolve_catalog_item(cleaned.get("catalog_product"))
        if item:
            cleaned["name"] = item["name"]
            cleaned["category"] = item["category"]
            if not cleaned.get("brand") and item.get("brand") and str(item.get("brand")).strip().casefold() not in {"universal", "general"}:
                cleaned["brand"] = item["brand"]
        elif not cleaned.get("name"):
            self.add_error("name", "Choose a catalogue product or enter a custom product name.")
        return cleaned

    def save(self, commit=True):
        product = super().save(commit=False)
        item = resolve_catalog_item(self.cleaned_data.get("catalog_product"))
        if item:
            product.name = item["name"]
            product.category = item["category"]
            if not product.brand and item.get("brand") and str(item.get("brand")).strip().casefold() not in {"universal", "general"}:
                product.brand = item["brand"]

        uploaded_main = self.files.get("image")
        if uploaded_main:
            enhanced = enhance_product_image(uploaded_main, product.name)
            try:
                # Store the Cloudinary public ID as text in CloudinaryField.
                product.image = upload_product_image(enhanced, product.name)
            except Exception as exc:
                self.add_error("image", f"Product photo could not be uploaded to Cloudinary. {exc}")
                raise forms.ValidationError("Product photo upload failed. Please check the Cloudinary deployment settings and try again.")

        if not product.sku:
            prefix = slugify(product.name or "product").replace("-", "").upper()[:24] or "PRODUCT"
            product.sku = f"SPV-{prefix}-{uuid.uuid4().hex[:8].upper()}"

        product._shopiva_gallery_files = self.cleaned_data.get("gallery_images", [])[:8]

        if commit:
            product.save()
        return product


class ProductReviewForm(forms.ModelForm):
    class Meta:
        model = ProductReview
        fields = ("rating", "comment")
        widgets = {
            "rating": forms.Select(choices=[(i, f"{i} / 5") for i in range(5, 0, -1)]),
            "comment": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Tell other shoppers about your experience.",
                }
            ),
        }
