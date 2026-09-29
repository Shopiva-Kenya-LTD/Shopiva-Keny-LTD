from decimal import Decimal
import json
import os
import uuid

from django import forms
from django.contrib import admin
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import ProtectedError, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from .admin_operations import admin_operations_center
from support.views import support_admin_center
from .payments import _create_seller_settlements
from .delivery_payouts import cancel_delivery_payout, complete_delivery_payout, fail_delivery_payout
from .notifications import notify_user
from .notification_service import notify_wishlist_product_change
from .nia_admin import answer_admin_question
from .models import CustomerAddress, DeliveryAgent, DeliveryEarning, DeliveryPayProfile, DeliveryPayout, DeliveryWallet, Order, OrderEvent, OrderItem, PaymentTransaction, Product, SellerPayoutRequest, SellerProfile, SellerSettlement, SellerWallet, WishlistItem, ProductReview, Notification, NotificationDelivery, DeliveryTariff, DeliveryHub, DeliveryPricingProfile, DeliveryPickupPoint, DeliveryRateCard, ShopivaBranch, ShopivaOutlet, NiaCallSession, NiaTask, NiaCallerVerification, NiaAuditLog


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = (
            "name",
            "sku",
            "description",
            "category",
            "price",
            "discount_percent",
            "stock_quantity",
            "promo_text",
            "image",
            "is_featured",
            "is_active",
            "seller",
            "package_class",
            "shipping_weight_kg",
            "package_length_cm",
            "package_width_cm",
            "package_height_cm",
            "fulfillment_ready",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "promo_text": forms.TextInput(attrs={"placeholder": "Optional promotion text"}),
        }


class ShopivaAdminSite(admin.AdminSite):
    site_header = "Shopiva Control Center"
    site_title = "Shopiva Admin"
    index_title = "Store Operations"
    index_template = "admin/shopiva_index.html"

    def _stats(self):
        today = timezone.localdate()
        orders = Order.objects.all()
        products = Product.objects.all()
        return {
            "products": products.count(),
            "active_products": products.filter(is_active=True).count(),
            "low_stock": products.filter(stock_quantity__lte=5, is_active=True).count(),
            "orders": orders.count(),
            "pending_orders": orders.filter(status="pending").count(),
            "processing_orders": orders.filter(status__in=["confirmed", "paid", "packed", "processing", "shipped"]).count(),
            "delivered_orders": orders.filter(status="delivered").count(),
            "assigned_orders": orders.exclude(delivery_agent__isnull=True).exclude(status__in=["delivered", "cancelled"]).count(),
            "today_orders": orders.filter(created_at__date=today).count(),
            "revenue": orders.exclude(status="cancelled").aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00"),
            "pending_staff_approvals": DeliveryAgent.objects.filter(is_active=False).count(),
        }

    def index(self, request, extra_context=None):
        extra_context = extra_context or {}
        orders = Order.objects.select_related("delivery_agent").all()
        products = Product.objects.all()
        extra_context.update(
            {
                "shopiva_stats": self._stats(),
                "recent_orders": orders.order_by("-created_at")[:8],
                "low_stock_products": products.filter(stock_quantity__lte=5, is_active=True).order_by("stock_quantity", "name")[:8],
                "recent_products": products.order_by("-id")[:6],
                "active_delivery_agents": DeliveryAgent.objects.filter(is_active=True).select_related("user").order_by("user__username"),
                "pending_staff_approvals": DeliveryAgent.objects.filter(is_active=False).select_related("user").order_by("-created_at")[:8],
            }
        )
        # The Shopiva dashboard is fully custom and does not depend on Django's
        # generated app list. Rendering it directly keeps a broken model registration
        # from taking down the entire Admin Control Center.
        # This dashboard is fully custom. Do not build Django's model/app list
        # here: a broken optional ModelAdmin must never take down /admin/.
        context = {
            "site_title": self.site_title,
            "site_header": self.site_header,
            "site_url": self.site_url,
            "has_permission": self.has_permission(request),
            "available_apps": [],
            "is_popup": False,
            "is_nav_sidebar": False,
            "title": self.index_title,
            **extra_context,
        }
        return TemplateResponse(request, self.index_template, context)

    def logout(self, request, extra_context=None):
        auth_logout(request)
        return redirect("shopiva_admin:login")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("products/", self.admin_view(self.product_manager), name="product_manager"),
            path("products/add/", self.admin_view(self.product_add), name="product_add"),
            # Backward-compatible routes used by older dashboard links.
            path("home/product/", self.admin_view(self.product_manager), name="legacy_product_manager"),
            path("home/product/add/", self.admin_view(self.product_add), name="legacy_product_add"),
            path("products/<int:product_id>/edit/", self.admin_view(self.product_edit), name="product_edit"),
            path("products/<int:product_id>/delete/", self.admin_view(self.product_delete), name="product_delete"),
            path("delivery-map/", self.admin_view(self.delivery_map), name="delivery_map"),
            path("delivery-locations/", self.admin_view(self.delivery_locations), name="delivery_locations"),
            path("approval-center/", self.admin_view(admin_operations_center), name="approval_center"),
            path("support-center/", self.admin_view(support_admin_center), name="support_center"),
            path("operations-center/", self.admin_view(admin_operations_center), name="operations_center"),
            path("nia-assistant/", self.admin_view(self.nia_assistant), name="nia_assistant"),
        ]
        return custom_urls + urls

    def nia_assistant(self, request):
        if request.method != "POST":
            return JsonResponse({"ok": False, "error": "Method not allowed."}, status=405)

        question = (request.POST.get("question") or "").strip()
        if not question:
            return JsonResponse({"ok": False, "error": "Ask Nia a question first."}, status=400)
        if len(question) > 1000:
            return JsonResponse({"ok": False, "error": "Question is too long."}, status=400)

        try:
            result = answer_admin_question(question)
            return JsonResponse({
                "ok": True,
                "answer": result.get("answer", "I could not find an answer for that question."),
            })
        except Exception:
            return JsonResponse({
                "ok": False,
                "error": "Nia could not complete that request right now. Please try again.",
            }, status=500)

    def delivery_map(self, request):
        counties = [
            "Nairobi", "Mombasa", "Kisumu", "Nakuru", "Uasin Gishu", "Kiambu",
            "Machakos", "Kajiado", "Nyeri", "Meru", "Kakamega", "Kilifi",
            "Bungoma", "Kericho", "Kisii", "Homa Bay", "Siaya", "Trans Nzoia",
            "Nandi", "Bomet", "Narok", "Laikipia", "Nyandarua", "Murang'a",
            "Embu", "Tharaka Nithi", "Kitui", "Makueni", "Taita Taveta",
            "Kwale", "Lamu", "Tana River", "Garissa", "Wajir", "Mandera",
            "Marsabit", "Isiolo", "Samburu", "Turkana", "West Pokot", "Elgeyo-Marakwet",
            "Baringo", "Vihiga", "Busia", "Migori", "Nyamira", "Kirinyaga",
        ]
        selected_county = request.GET.get("county", "").strip()
        active_qs = Order.objects.select_related("delivery_agent").filter(
            delivery_agent__isnull=False,
            status__in=["confirmed", "paid", "packed", "processing", "shipped", "out_for_delivery"],
        )
        if selected_county:
            active_qs = active_qs.filter(address__icontains=selected_county)
        active_orders = list(active_qs.order_by("-created_at")[:50])

        now = timezone.now()
        rider_qs = DeliveryAgent.objects.filter(is_active=True).select_related("user")
        rider_rows = []
        for agent in rider_qs:
            latest_order = (
                agent.orders.select_related("delivery_agent")
                .exclude(status__in=["cancelled"])
                .order_by("-created_at")
                .first()
            )
            if selected_county and latest_order and selected_county.lower() not in (latest_order.address or "").lower():
                continue
            live = agent.location_is_live
            stale_minutes = None
            if agent.last_location_at:
                stale_minutes = max(0, int((now - agent.last_location_at).total_seconds() // 60))
            rider_rows.append(
                {
                    "agent": agent,
                    "latest_order": latest_order,
                    "live": live,
                    "stale_minutes": stale_minutes,
                }
            )

        active_count = len(active_orders)
        online_count = sum(1 for row in rider_rows if row["agent"].status in {"available", "on_delivery"} and row["live"])
        delayed_count = sum(
            1 for order in active_orders
            if order.delivery_agent and order.delivery_agent.last_location_at and not order.delivery_agent.location_is_live
        )
        on_time_count = max(0, active_count - delayed_count)
        today_deliveries = Order.objects.filter(created_at__date=timezone.localdate(), delivery_agent__isnull=False)

        context = {
            **self.each_context(request),
            "shopiva_stats": self._stats(),
            "delivery_agents": rider_qs,
            "rider_rows": rider_rows,
            "recent_deliveries": active_orders[:8] + list(
                Order.objects.select_related("delivery_agent").filter(delivery_agent__isnull=False).order_by("-created_at")[:8]
            ),
            "counties": counties,
            "selected_county": selected_county,
            "delivery_dashboard": {
                "active": active_count,
                "on_time": on_time_count,
                "delayed": delayed_count,
                "riders_online": online_count,
                "today": today_deliveries.count(),
            },
        }
        return TemplateResponse(request, "admin/delivery_map.html", context)

    def delivery_locations(self, request):
        county = request.GET.get("county", "").strip()
        agents = DeliveryAgent.objects.filter(is_active=True).select_related("user")
        data = []
        for agent in agents:
            if agent.current_latitude is None or agent.current_longitude is None:
                continue
            latest_order = agent.orders.select_related("delivery_agent").exclude(status="cancelled").order_by("-created_at").first()
            address = latest_order.address if latest_order else ""
            if county and county.lower() not in address.lower():
                continue
            data.append(
                {
                    "id": agent.id,
                    "name": agent.display_name,
                    "phone": agent.phone,
                    "vehicle_type": agent.vehicle_type,
                    "vehicle_number": agent.vehicle_number,
                    "status": agent.get_status_display(),
                    "status_code": agent.status,
                    "live": agent.location_is_live,
                    "latitude": float(agent.current_latitude),
                    "longitude": float(agent.current_longitude),
                    "updated": agent.last_location_at.isoformat() if agent.last_location_at else None,
                    "order_id": latest_order.id if latest_order else None,
                    "tracking_code": latest_order.tracking_code if latest_order else "",
                    "order_status": latest_order.get_status_display() if latest_order else "No active order",
                    "address": address[:120],
                }
            )
        return JsonResponse({"agents": data, "updated_at": timezone.now().isoformat()})

    def product_manager(self, request):
        query = request.GET.get("q", "").strip()
        category = request.GET.get("category", "").strip()
        status = request.GET.get("status", "").strip()

        product_list = Product.objects.all().order_by("-id")
        if query:
            product_list = product_list.filter(name__icontains=query)
        if category:
            product_list = product_list.filter(category__iexact=category)
        if status == "active":
            product_list = product_list.filter(is_active=True)
        elif status == "inactive":
            product_list = product_list.filter(is_active=False)
        elif status == "low":
            product_list = product_list.filter(is_active=True, stock_quantity__lte=5)

        categories = (
            Product.objects.exclude(category="")
            .values_list("category", flat=True)
            .distinct()
            .order_by("category")
        )

        context = {
            **self.each_context(request),
            "products": product_list,
            "categories": categories,
            "query": query,
            "selected_category": category,
            "selected_status": status,
            "product_count": product_list.count(),
        }
        return TemplateResponse(request, "admin/products/manager.html", context)

    def product_add(self, request):
        if request.method == "POST":
            form = ProductForm(request.POST, request.FILES)
            if form.is_valid():
                form.save()
                return redirect("shopiva_admin:product_manager")
        else:
            form = ProductForm()

        context = {**self.each_context(request), "form": form, "page_title": "Add Product", "mode": "add"}
        return TemplateResponse(request, "admin/products/form.html", context)

    def product_edit(self, request, product_id):
        product = get_object_or_404(Product, id=product_id)
        if request.method == "POST":
            form = ProductForm(request.POST, request.FILES, instance=product)
            if form.is_valid():
                form.save()
                return redirect("shopiva_admin:product_manager")
        else:
            form = ProductForm(instance=product)

        context = {
            **self.each_context(request),
            "form": form,
            "product": product,
            "page_title": "Edit Product",
            "mode": "edit",
        }
        return TemplateResponse(request, "admin/products/form.html", context)

    def product_delete(self, request, product_id):
        product = get_object_or_404(Product, id=product_id)
        if request.method == "POST":
            try:
                product.delete()
            except ProtectedError:
                product.is_active = False
                product.save(update_fields=["is_active"])
            return redirect("shopiva_admin:product_manager")

        context = {**self.each_context(request), "product": product}
        return TemplateResponse(request, "admin/products/delete.html", context)


    def save_model(self, request, obj, form, change):
        previous = Product.objects.get(pk=obj.pk) if change and obj.pk else None
        old_price = previous.price if previous else obj.price
        old_discount = previous.discount_percent if previous else obj.discount_percent
        old_stock = previous.stock_quantity if previous else 0
        super().save_model(request, obj, form, change)
        if previous is not None:
            notify_wishlist_product_change(
                obj, old_price, old_discount, old_stock, actor_label="Shopiva admin"
            )


# Custom Shopiva admin site used by all registered models and /admin/.
shopiva_admin_site = ShopivaAdminSite(name="shopiva_admin")


@admin.register(Order, site=shopiva_admin_site)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "customer_name",
        "total_amount",
        "status",
        "payment_status",
        "delivery_agent",
        "tracking_code",
        "created_at",
    )
    list_filter = ("status", "payment_status", "delivery_agent", "created_at")
    search_fields = ("customer_name", "email", "phone", "address", "tracking_code", "payment_reference")
    ordering = ("-created_at",)
    list_per_page = 25
    readonly_fields = (
        "tracking_code", "packed_at", "paid_at", "assigned_at",
        "delivery_confirmation_code", "delivery_verification_attempts",
        "delivery_verification_locked_at", "delivered_at",
    )

    def save_model(self, request, obj, form, change):
        previous = None
        if change and obj.pk:
            previous = Order.objects.get(pk=obj.pk)

        if not obj.tracking_code:
            obj.tracking_code = f"SPV-{uuid.uuid4().hex[:10].upper()}"

        now = timezone.now()
        if obj.status == "packed" and not obj.packed_at:
            obj.packed_at = now
        if obj.payment_status == "paid" and not obj.paid_at:
            obj.paid_at = now
        if obj.delivery_agent_id and not obj.assigned_at:
            obj.assigned_at = now
        if obj.delivery_agent_id:
            obj.ensure_delivery_confirmation_code()
        if obj.status == "delivered" and not obj.delivered_at:
            obj.delivered_at = now

        super().save_model(request, obj, form, change)

        if previous is None:
            OrderEvent.objects.create(
                order=obj,
                event_type="placed",
                note="Order created in the Shopiva control center.",
                actor=request.user,
            )
            return

        if previous.status != obj.status and obj.status == "delivered":
            with transaction.atomic():
                for settlement in SellerSettlement.objects.select_for_update().filter(order=obj, status="pending"):
                    wallet = SellerWallet.objects.select_for_update().get(seller=settlement.seller)
                    wallet.pending_balance = max(Decimal("0.00"), wallet.pending_balance - settlement.seller_amount)
                    wallet.available_balance += settlement.seller_amount
                    wallet.save(update_fields=("pending_balance", "available_balance", "updated_at"))
                    settlement.status = "available"
                    settlement.released_at = now
                    settlement.save(update_fields=("status", "released_at"))
                    notify_user(settlement.seller.user, "Seller earnings released", f"Order {obj.tracking_code} was delivered. KSh {settlement.seller_amount:,.2f} is now available for payout.", "delivery", "/seller/")
        if previous.status != obj.status:
            event_map = {
                "confirmed": "confirmed",
                "packed": "packed",
                "processing": "processing",
                "shipped": "shipped",
                "out_for_delivery": "out_for_delivery",
                "delivered": "delivered",
                "cancelled": "cancelled",
            }
            event_type = event_map.get(obj.status)
            if event_type:
                OrderEvent.objects.create(
                    order=obj,
                    event_type=event_type,
                    note=f"Order status changed to {obj.get_status_display()}.",
                    actor=request.user,
                    delivery_agent=obj.delivery_agent,
                )

        if previous.payment_status != obj.payment_status and obj.payment_status == "paid":
            _create_seller_settlements(obj)
            OrderEvent.objects.create(
                order=obj,
                event_type="paid",
                note=f"Payment confirmed{(' - ' + obj.payment_reference) if obj.payment_reference else ''}.",
                actor=request.user,
                delivery_agent=obj.delivery_agent,
            )

        if previous.delivery_agent_id != obj.delivery_agent_id and obj.delivery_agent:
            OrderEvent.objects.create(
                order=obj,
                event_type="assigned",
                note=f"Assigned to {obj.delivery_agent.display_name}.",
                actor=request.user,
                delivery_agent=obj.delivery_agent,
            )


@admin.register(OrderItem, site=shopiva_admin_site)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "product", "quantity", "price")
    search_fields = ("product__name",)
    ordering = ("-id",)
    list_per_page = 25


@admin.register(DeliveryAgent, site=shopiva_admin_site)
class DeliveryAgentAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "phone",
        "vehicle_type",
        "vehicle_number",
        "approval_state",
        "status",
        "is_active",
        "last_location_at",
    )
    list_filter = ("status", "is_active", "vehicle_type")
    search_fields = ("user__username", "user__first_name", "user__last_name", "phone", "vehicle_number")
    list_editable = ("status",)
    readonly_fields = ("current_latitude", "current_longitude", "last_location_at", "is_active")
    list_per_page = 25

    @admin.display(description="Access state", boolean=False)
    def approval_state(self, obj):
        return "Approved / active" if obj.is_active else "Pending Shopiva approval"

    def save_model(self, request, obj, form, change):
        # Delivery accounts created from the admin are applications, not
        # pre-approved staff. The Approval Center is the single activation path.
        if not change:
            obj.is_active = False
            obj.status = "offline"

        super().save_model(request, obj, form, change)

        if obj.user_id:
            obj.user.is_active = bool(obj.is_active)
            obj.user.save(update_fields=("is_active",))

        if not change and obj.user_id:
            approval_url = reverse("shopiva_admin:approval_center")
            admins = User.objects.filter(is_staff=True, is_active=True)
            for admin_user in admins:
                notify_user(
                    admin_user,
                    "system",
                    "New staff approval required",
                    (
                        f"{obj.display_name} was added as a delivery staff application. "
                        "Open the Approval Center to verify the applicant before activation."
                    ),
                    link=approval_url,
                )


@admin.register(DeliveryPayProfile, site=shopiva_admin_site)
class DeliveryPayProfileAdmin(admin.ModelAdmin):
    list_display = (
        "name", "base_per_delivery", "per_km_rate", "minimum_payout",
        "auto_payout_threshold", "auto_payout_enabled", "is_active", "updated_at",
    )
    list_filter = ("is_active", "auto_payout_enabled")
    search_fields = ("name", "notes")
    list_editable = (
        "base_per_delivery", "per_km_rate", "minimum_payout",
        "auto_payout_threshold", "auto_payout_enabled", "is_active",
    )
    ordering = ("-is_active", "-updated_at")
    fieldsets = (
        ("Rider earnings", {"fields": ("name", "base_per_delivery", "per_km_rate")}),
        ("Payout policy", {"fields": ("minimum_payout", "auto_payout_enabled", "auto_payout_threshold")}),
        ("Governance", {"fields": ("is_active", "notes")}),
    )


@admin.register(DeliveryWallet, site=shopiva_admin_site)
class DeliveryWalletAdmin(admin.ModelAdmin):
    list_display = (
        "agent", "payout_phone", "available_balance", "pending_payout_balance",
        "total_earned", "total_paid", "auto_payout_enabled", "last_payout_at",
    )
    list_filter = ("auto_payout_enabled",)
    search_fields = ("agent__user__username", "agent__user__email", "agent__phone", "payout_phone")
    list_editable = ("payout_phone", "auto_payout_enabled")
    readonly_fields = ("agent", "available_balance", "pending_payout_balance", "total_earned", "total_paid", "last_payout_at", "updated_at")
    list_per_page = 25


@admin.register(DeliveryEarning, site=shopiva_admin_site)
class DeliveryEarningAdmin(admin.ModelAdmin):
    list_display = (
        "id", "agent", "order", "distance_km", "base_amount",
        "distance_amount", "total_amount", "status", "distance_source", "earned_at",
    )
    list_filter = ("status", "distance_source", "pay_profile")
    search_fields = ("agent__user__username", "agent__user__email", "order__tracking_code")
    readonly_fields = (
        "agent", "order", "pay_profile", "distance_km", "distance_source",
        "base_amount", "distance_amount", "total_amount", "status", "payout", "earned_at",
    )
    ordering = ("-earned_at",)
    list_per_page = 50


@admin.register(DeliveryPayout, site=shopiva_admin_site)
class DeliveryPayoutAdmin(admin.ModelAdmin):
    list_display = (
        "id", "agent", "amount", "phone", "status", "trigger", "provider",
        "provider_reference", "created_at", "processed_at", "paid_at",
    )
    list_filter = ("status", "trigger", "provider", "created_at")
    search_fields = (
        "agent__user__username", "agent__user__email", "phone",
        "provider_reference", "idempotency_key",
    )
    readonly_fields = (
        "agent", "amount", "phone", "trigger", "provider",
        "provider_reference", "provider_response", "idempotency_key",
        "created_at", "updated_at", "processed_at", "paid_at",
    )
    list_per_page = 25

    def save_model(self, request, obj, form, change):
        previous_status = None
        if change and obj.pk:
            previous_status = DeliveryPayout.objects.get(pk=obj.pk).status
        super().save_model(request, obj, form, change)
        if not change or previous_status == obj.status:
            return
        if obj.status == DeliveryPayout.STATUS_PAID:
            complete_delivery_payout(obj, provider_reference=obj.provider_reference, provider_response=obj.provider_response)
            notify_user(
                obj.agent.user,
                "payout",
                "Delivery payout confirmed",
                f"Your Shopiva delivery payout #{obj.id} for KSh {obj.amount:,.2f} has been marked paid.",
                link="/delivery/payouts/",
                phone=obj.agent.phone,
            )
        elif obj.status == DeliveryPayout.STATUS_FAILED:
            fail_delivery_payout(obj, obj.failure_reason or "Payout marked failed by Shopiva admin.")
        elif obj.status == DeliveryPayout.STATUS_CANCELLED:
            cancel_delivery_payout(obj, obj.failure_reason or "Payout cancelled by Shopiva admin.")


@admin.register(ShopivaBranch, site=shopiva_admin_site)
class ShopivaBranchAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "county", "town", "phone", "is_headquarters", "is_active", "updated_at")
    list_filter = ("county", "is_headquarters", "is_active")
    search_fields = ("name", "code", "county", "town", "address", "phone", "email")
    list_editable = ("is_headquarters", "is_active")
    ordering = ("county", "town", "name")
    fieldsets = (
        ("Branch identity", {"fields": ("name", "code", "is_headquarters", "is_active")}),
        ("Location", {"fields": ("county", "town", "address", "latitude", "longitude")}),
        ("Contact", {"fields": ("phone", "email")}),
        ("Operations", {"fields": ("opening_time", "closing_time", "services")}),
    )


@admin.register(ShopivaOutlet, site=shopiva_admin_site)
class ShopivaOutletAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "branch", "county", "town", "phone", "pickup_available", "is_active", "updated_at")
    list_filter = ("county", "pickup_available", "is_active")
    search_fields = ("name", "code", "branch__name", "county", "town", "address", "phone", "email")
    list_editable = ("pickup_available", "is_active")
    ordering = ("county", "town", "name")
    autocomplete_fields = ("branch",)
    fieldsets = (
        ("Outlet identity", {"fields": ("name", "code", "branch", "is_active")}),
        ("Location", {"fields": ("county", "town", "address", "latitude", "longitude")}),
        ("Contact", {"fields": ("phone", "email")}),
        ("Operations", {"fields": ("opening_time", "closing_time", "services", "pickup_available")}),
    )


@admin.register(DeliveryHub, site=shopiva_admin_site)
class DeliveryHubAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "county", "town", "fulfillment_model", "is_primary", "is_active", "updated_at")
    list_filter = ("county", "fulfillment_model", "is_primary", "is_active")
    search_fields = ("name", "code", "county", "town", "address")
    list_editable = ("fulfillment_model", "is_primary", "is_active")
    ordering = ("-is_primary", "name")


@admin.register(DeliveryPricingProfile, site=shopiva_admin_site)
class DeliveryPricingProfileAdmin(admin.ModelAdmin):
    list_display = (
        "name", "delivery_mode", "pricing_mode", "base_fee", "per_km_fee",
        "per_seller_fee", "rural_surcharge", "minimum_fee", "maximum_fee",
        "rounding_step", "is_active", "updated_at",
    )
    list_filter = ("delivery_mode", "pricing_mode", "is_active")
    search_fields = ("name", "notes")
    list_editable = (
        "base_fee", "per_km_fee", "per_seller_fee", "rural_surcharge",
        "minimum_fee", "maximum_fee", "rounding_step", "is_active",
    )
    ordering = ("delivery_mode", "name")
    list_per_page = 50


@admin.register(DeliveryPickupPoint, site=shopiva_admin_site)
class DeliveryPickupPointAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "county", "town", "partner_name", "max_holding_days", "is_active", "updated_at")
    list_filter = ("county", "is_active")
    search_fields = ("name", "code", "county", "town", "address", "partner_name")
    list_editable = ("max_holding_days", "is_active")
    ordering = ("county", "town", "name")
    list_per_page = 100


@admin.register(DeliveryRateCard, site=shopiva_admin_site)
class DeliveryRateCardAdmin(admin.ModelAdmin):
    list_display = (
        "name", "fulfillment_model", "delivery_mode", "package_class", "route_class",
        "base_fee", "per_km_fee", "per_extra_seller_fee", "minimum_fee",
        "maximum_fee", "rounding_step", "is_active", "updated_at",
    )
    list_filter = ("fulfillment_model", "delivery_mode", "package_class", "route_class", "is_active")
    search_fields = ("name", "notes")
    list_editable = (
        "base_fee", "per_km_fee", "per_extra_seller_fee", "minimum_fee",
        "maximum_fee", "rounding_step", "is_active",
    )
    ordering = ("delivery_mode", "package_class", "route_class", "name")
    list_per_page = 100


@admin.register(DeliveryTariff, site=shopiva_admin_site)
class DeliveryTariffAdmin(admin.ModelAdmin):
    list_display = ("county", "destination", "standard_fee", "is_fallback", "pickup_fee", "express_fee", "pickup_available", "express_available", "is_active", "updated_at")
    list_filter = ("county", "is_fallback", "pickup_available", "express_available", "is_active")
    search_fields = ("county", "destination")
    list_editable = ("standard_fee", "pickup_fee", "express_fee", "pickup_available", "express_available", "is_active")
    ordering = ("county", "destination")
    list_per_page = 100


@admin.register(CustomerAddress, site=shopiva_admin_site)
class CustomerAddressAdmin(admin.ModelAdmin):
    list_display = ("label", "full_name", "phone", "town", "county", "is_default", "user")
    list_filter = ("county", "is_default")
    search_fields = ("full_name", "phone", "town", "county", "address_line", "user__username", "user__email")
    list_editable = ("is_default",)


@admin.register(WishlistItem, site=shopiva_admin_site)
class WishlistItemAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "created_at")
    search_fields = ("user__username", "user__email", "product__name")
    ordering = ("-created_at",)


@admin.register(OrderEvent, site=shopiva_admin_site)
class OrderEventAdmin(admin.ModelAdmin):
    list_display = ("order", "event_type", "delivery_agent", "actor", "created_at")
    list_filter = ("event_type", "delivery_agent", "created_at")
    search_fields = ("order__customer_name", "order__tracking_code", "note", "actor__username")
    readonly_fields = ("order", "event_type", "note", "actor", "delivery_agent", "created_at")
    ordering = ("-created_at",)


@admin.register(User, site=shopiva_admin_site)
class ShopivaUserAdmin(UserAdmin):
    list_display = ("username", "email", "is_staff", "is_active", "date_joined")
    search_fields = ("username", "email")
    list_filter = ("is_staff", "is_active", "is_superuser")


@admin.register(Group, site=shopiva_admin_site)
class ShopivaGroupAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(SellerProfile, site=shopiva_admin_site)
class SellerProfileAdmin(admin.ModelAdmin):
    list_display = ("business_name", "user", "mpesa_phone", "commission_percent", "is_active", "created_at")
    list_filter = ("is_active", "commission_percent")
    search_fields = ("business_name", "user__username", "user__email", "mpesa_phone")
    list_editable = ("commission_percent", "is_active")

@admin.register(SellerWallet, site=shopiva_admin_site)
class SellerWalletAdmin(admin.ModelAdmin):
    list_display = ("seller", "pending_balance", "available_balance", "total_sales", "total_commission", "updated_at")
    search_fields = ("seller__business_name", "seller__user__username", "seller__user__email")
    readonly_fields = ("pending_balance", "available_balance", "total_sales", "total_commission", "updated_at")

@admin.register(SellerSettlement, site=shopiva_admin_site)
class SellerSettlementAdmin(admin.ModelAdmin):
    list_display = ("order", "seller", "gross_amount", "platform_commission", "seller_amount", "status", "provider_reference", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("order__tracking_code", "seller__business_name", "seller__user__username", "provider_reference")
    readonly_fields = ("order", "seller", "gross_amount", "platform_commission", "seller_amount", "provider_reference", "created_at", "released_at", "paid_at")

@admin.register(SellerPayoutRequest, site=shopiva_admin_site)
class SellerPayoutRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "seller", "amount", "phone", "status", "provider_reference", "created_at", "paid_at")
    list_filter = ("status", "created_at")
    search_fields = ("seller__business_name", "seller__user__username", "seller__user__email", "phone", "provider_reference", "idempotency_key")
    readonly_fields = ("seller", "amount", "phone", "idempotency_key", "provider_response", "created_at", "updated_at", "paid_at")
    list_editable = ("status",)
    list_per_page = 25

    def save_model(self, request, obj, form, change):
        previous_status = None
        if change and obj.pk:
            previous_status = SellerPayoutRequest.objects.get(pk=obj.pk).status
        super().save_model(request, obj, form, change)
        if not change or previous_status == obj.status:
            return
        with transaction.atomic():
            wallet = SellerWallet.objects.select_for_update().get(seller=obj.seller)
            if obj.status == "paid":
                obj.paid_at = timezone.now()
                obj.save(update_fields=("paid_at", "updated_at"))
                notify_user(obj.seller.user, "Seller payout confirmed", f"Your Shopiva payout #{obj.id} for KSh {obj.amount:,.2f} has been marked paid by the admin.", "payout", "/seller/")
            elif obj.status in {"failed", "cancelled"} and previous_status not in {"failed", "cancelled"}:
                wallet.available_balance += obj.amount
                wallet.save(update_fields=("available_balance", "updated_at"))
                obj.save(update_fields=("updated_at",))


@admin.register(ProductReview, site=shopiva_admin_site)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ("product", "customer", "order", "rating", "created_at")
    list_filter = ("rating", "created_at")
    search_fields = ("product__name", "customer__username", "customer__email", "comment")
    readonly_fields = ("product", "customer", "order", "rating", "comment", "created_at", "updated_at")


@admin.register(Notification, site=shopiva_admin_site)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "notification_type", "title", "is_read", "created_at")
    list_filter = ("notification_type", "is_read", "created_at")
    search_fields = ("user__username", "user__email", "title", "message")
    list_editable = ("is_read",)
    ordering = ("-created_at",)


@admin.register(NotificationDelivery, site=shopiva_admin_site)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ("notification", "channel", "status", "provider_status", "provider_message_id", "created_at", "delivered_at")
    list_filter = ("channel", "status", "created_at")
    search_fields = ("notification__user__username", "notification__user__email", "provider_message_id", "provider_status", "error_message")
    readonly_fields = ("notification", "channel", "status", "provider_status", "provider_message_id", "error_message", "created_at", "updated_at", "delivered_at")
    ordering = ("-created_at",)



@admin.register(NiaCallSession, site=shopiva_admin_site)
class NiaCallSessionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "role", "direction", "phone_e164", "caller_verified", "status", "provider_sid", "updated_at")
    list_filter = ("role", "status", "provider")
    search_fields = ("phone_e164", "provider_sid", "last_user_text", "last_ai_text")
    readonly_fields = ("id", "user", "role", "phone_e164", "status", "provider", "provider_sid", "conversation", "last_user_text", "last_ai_text", "created_at", "updated_at")


@admin.register(NiaCallerVerification, site=shopiva_admin_site)
class NiaCallerVerificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "role", "phone_e164", "attempts", "expires_at", "verified_at", "used_at")
    list_filter = ("role", "verified_at", "used_at", "expires_at")
    search_fields = ("phone_e164", "user__username", "user__email")
    readonly_fields = (
        "id", "user", "role", "phone_e164", "attempts",
        "expires_at", "verified_at", "used_at", "created_at", "updated_at",
    )
    exclude = ("pin_code",)


@admin.register(NiaAuditLog, site=shopiva_admin_site)
class NiaAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "role", "action", "user")
    list_filter = ("role", "action", "created_at")
    search_fields = ("action", "user__username", "user__email")
    readonly_fields = ("user", "role", "action", "detail", "created_at")
    ordering = ("-created_at",)


@admin.register(NiaTask, site=shopiva_admin_site)
class NiaTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "role", "trigger_kind", "enabled", "next_run_at", "last_run_at")
    list_filter = ("role", "trigger_kind", "enabled")
    search_fields = ("title", "instruction", "user__username", "user__email")
    readonly_fields = ("last_run_at", "last_result", "created_at", "updated_at")
    ordering = ("enabled", "next_run_at", "-created_at")