from decimal import Decimal, InvalidOperation
from django.conf import settings
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import SellerProductForm
from .media_authenticity import screen_image, screen_video
from .media_pipeline import enhance_product_image
from .indexnow import submit_urls
from .models import CustomerAddress, DeliveryAgent, DeliveryLocationPing, Order, Product
from .models_product_media import ProductMedia, ProductVideo

logger = logging.getLogger(__name__)



def _notify_product_indexnow(product):
    site = str(getattr(settings, "PUBLIC_SITE_URL", "https://shopivakenya.top") or "https://shopivakenya.top").rstrip("/")
    submit_urls([f"{site}/product/{product.id}/"])

def _coordinate(value, minimum, maximum):
    try:
        coordinate = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    if not (minimum <= coordinate <= maximum):
        return None
    return coordinate.quantize(Decimal("0.000001"))


def _seller_location(request):
    return (
        request.POST.get("business_address", "").strip(),
        _coordinate(request.POST.get("business_latitude"), Decimal("-90"), Decimal("90")),
        _coordinate(request.POST.get("business_longitude"), Decimal("-180"), Decimal("180")),
    )


def _seller_location_error(address, latitude, longitude):
    if not address:
        return "Choose the business or pickup address from Google Maps."
    if latitude is None or longitude is None:
        return "Select an exact map pin or use your current location before saving."
    return None


def seller_product_add_map(request):
    seller = getattr(request.user, "seller_profile", None)
    if not seller or not seller.is_active:
        return redirect("seller_register")

    if request.method == "POST":
        form = SellerProductForm(request.POST, request.FILES)
        business_address, latitude, longitude = _seller_location(request)
        location_error = _seller_location_error(business_address, latitude, longitude)
        if location_error:
            messages.error(request, location_error)
        elif form.is_valid():
            try:
                product = form.save(commit=False)
            except ValidationError as exc:
                form.add_error("image", exc)
            else:
                product.seller = seller
                product.is_active = True
                try:
                    with transaction.atomic():
                        seller.business_address = business_address
                        seller.business_latitude = latitude
                        seller.business_longitude = longitude
                        seller.save(update_fields=["business_address", "business_latitude", "business_longitude"])
                        product.save()
                        gallery_files = getattr(product, "_shopiva_gallery_files", [])
                        if gallery_files:
                            product.media.all().delete()
                            for position, image_file in enumerate(gallery_files[:8]):
                                review = screen_image(image_file)
                                ProductMedia.objects.create(
                                    product=product,
                                    image=enhance_product_image(image_file, f"{product.name}-gallery-{position + 1}"),
                                    position=position,
                                    ai_status=review["status"],
                                    ai_score=review["score"],
                                    ai_notes=review["notes"],
                                )
                        video_files = getattr(product, "_shopiva_video_files", [])
                        if video_files:
                            product.videos.all().delete()
                            for position, video_file in enumerate(video_files[:5]):
                                review = screen_video(video_file)
                                ProductVideo.objects.create(
                                    product=product,
                                    video=video_file,
                                    position=position,
                                    ai_status=review["status"],
                                    ai_score=review["score"],
                                    ai_notes=review["notes"],
                                )
                except Exception as exc:
                    logger.exception("Seller product save failed", exc_info=exc)
                    messages.error(request, "The product could not be saved. Please correct the listing and try again.")
                else:
                    messages.success(request, f"{product.name} is now listed on Shopiva.")
                    _notify_product_indexnow(product)
                    return redirect("seller_dashboard")
    else:
        form = SellerProductForm()

    return render(
        request,
        "seller/product_form.html",
        {
            "form": form,
            "mode": "add",
            "location_address": seller.business_address,
            "location_latitude": seller.business_latitude,
            "location_longitude": seller.business_longitude,
        },
    )


def seller_product_edit_map(request, product_id):
    seller = getattr(request.user, "seller_profile", None)
    if not seller or not seller.is_active:
        return redirect("seller_register")
    product = get_object_or_404(Product, id=product_id, seller=seller)

    if request.method == "POST":
        form = SellerProductForm(request.POST, request.FILES, instance=product)
        business_address, latitude, longitude = _seller_location(request)
        location_error = _seller_location_error(business_address, latitude, longitude)
        if location_error:
            messages.error(request, location_error)
        elif form.is_valid():
            try:
                updated_product = form.save(commit=False)
            except ValidationError as exc:
                form.add_error("image", exc)
            else:
                try:
                    with transaction.atomic():
                        seller.business_address = business_address
                        seller.business_latitude = latitude
                        seller.business_longitude = longitude
                        seller.save(update_fields=["business_address", "business_latitude", "business_longitude"])
                        updated_product.save()
                        gallery_files = getattr(updated_product, "_shopiva_gallery_files", [])
                        if gallery_files:
                            updated_product.media.all().delete()
                            for position, image_file in enumerate(gallery_files[:8]):
                                review = screen_image(image_file)
                                ProductMedia.objects.create(
                                    product=updated_product,
                                    image=enhance_product_image(image_file, f"{updated_product.name}-gallery-{position + 1}"),
                                    position=position,
                                    ai_status=review["status"],
                                    ai_score=review["score"],
                                    ai_notes=review["notes"],
                                )
                        video_files = getattr(updated_product, "_shopiva_video_files", [])
                        if video_files:
                            updated_product.videos.all().delete()
                            for position, video_file in enumerate(video_files[:5]):
                                review = screen_video(video_file)
                                ProductVideo.objects.create(
                                    product=updated_product,
                                    video=video_file,
                                    position=position,
                                    ai_status=review["status"],
                                    ai_score=review["score"],
                                    ai_notes=review["notes"],
                                )
                except Exception as exc:
                    logger.exception("Seller product update failed", exc_info=exc)
                    messages.error(request, "The product update could not be completed. Please try again.")
                else:
                    messages.success(request, f"{updated_product.name} has been updated.")
                    _notify_product_indexnow(updated_product)
                    return redirect("seller_dashboard")
    else:
        form = SellerProductForm(instance=product)

    return render(
        request,
        "seller/product_form.html",
        {
            "form": form,
            "mode": "edit",
            "product": product,
            "location_address": seller.business_address,
            "location_latitude": seller.business_latitude,
            "location_longitude": seller.business_longitude,
        },
    )


@login_required(login_url="customer_login")
def customer_addresses_map(request):
    if request.user.is_staff or request.user.is_superuser:
        return redirect("/admin/")

    if request.method == "POST":
        action = request.POST.get("action", "save")
        address_id = request.POST.get("address_id")

        if action == "delete" and address_id:
            CustomerAddress.objects.filter(id=address_id, user=request.user).delete()
            messages.success(request, "Address removed.")
            return redirect("customer_addresses")

        if action == "default" and address_id:
            with transaction.atomic():
                CustomerAddress.objects.filter(user=request.user).update(is_default=False)
                CustomerAddress.objects.filter(id=address_id, user=request.user).update(is_default=True)
            messages.success(request, "Default delivery address updated.")
            return redirect("customer_addresses")

        latitude = _coordinate(request.POST.get("latitude"), Decimal("-90"), Decimal("90"))
        longitude = _coordinate(request.POST.get("longitude"), Decimal("-180"), Decimal("180"))
        fields = {
            "label": request.POST.get("label", "Home").strip() or "Home",
            "full_name": request.POST.get("full_name", "").strip(),
            "phone": request.POST.get("phone", "").strip(),
            "county": request.POST.get("county", "").strip(),
            "town": request.POST.get("town", "").strip(),
            "address_line": request.POST.get("address_line", "").strip(),
            "landmark": request.POST.get("landmark", "").strip(),
            "latitude": latitude,
            "longitude": longitude,
        }
        required = ("full_name", "phone", "county", "town", "address_line")
        if not all(fields[key] for key in required):
            messages.error(request, "Please complete your name, phone, county, town and address.")
        elif latitude is None or longitude is None:
            messages.error(request, "Pin the delivery location on Google Maps before saving this address.")
        else:
            with transaction.atomic():
                if not CustomerAddress.objects.filter(user=request.user).exists():
                    fields["is_default"] = True
                saved = CustomerAddress.objects.create(user=request.user, **fields)
                if saved.is_default:
                    CustomerAddress.objects.filter(user=request.user).exclude(id=saved.id).update(is_default=False)
            messages.success(request, "Delivery address saved with an exact map location.")
            return redirect("customer_addresses")

    addresses = CustomerAddress.objects.filter(user=request.user)
    return render(request, "accounts/addresses.html", {"addresses": addresses})


@login_required(login_url="customer_login")
def customer_delivery_location_map(request):
    if (
        request.user.is_staff
        or request.user.is_superuser
        or hasattr(request.user, "seller_profile")
        or hasattr(request.user, "delivery_agent_profile")
    ):
        return JsonResponse({"ok": False, "error": "Customer delivery tracking is only available to customer accounts."}, status=403)

    order_id = request.GET.get("order_id", "").strip()
    if order_id:
        latest_order = get_object_or_404(
            Order.objects.filter(customer=request.user)
            .select_related("delivery_agent")
            .prefetch_related("events"),
            id=order_id,
        )
    else:
        latest_order = (
            Order.objects.filter(customer=request.user)
            .select_related("delivery_agent")
            .prefetch_related("events")
            .order_by("-created_at")
            .first()
        )
    if not latest_order or not latest_order.delivery_agent:
        return JsonResponse({"ok": True, "agent": None, "order": None, "events": []})

    agent = latest_order.delivery_agent
    latest_ping = agent.location_history.order_by("-recorded_at").first()
    return JsonResponse(
        {
            "ok": True,
            "agent": {
                "id": agent.id,
                "name": agent.display_name,
                "phone": agent.phone or "",
                "vehicle_type": agent.vehicle_type or "",
                "vehicle_number": agent.vehicle_number or "",
                "status": agent.get_status_display(),
                "latitude": float(agent.current_latitude) if agent.current_latitude is not None else None,
                "longitude": float(agent.current_longitude) if agent.current_longitude is not None else None,
                "updated": agent.last_location_at.isoformat() if agent.last_location_at else None,
                "live": agent.location_is_live,
                "accuracy": float(latest_ping.accuracy_meters) if latest_ping and latest_ping.accuracy_meters is not None else None,
            },
            "order": {
                "id": latest_order.id,
                "tracking_code": latest_order.tracking_code,
                "status": latest_order.get_status_display(),
                "address": latest_order.address,
                "latitude": float(latest_order.delivery_latitude) if latest_order.delivery_latitude is not None else None,
                "longitude": float(latest_order.delivery_longitude) if latest_order.delivery_longitude is not None else None,
            },
            "events": [
                {
                    "type": event.event_type,
                    "label": event.get_event_type_display(),
                    "note": event.note or "",
                    "created": event.created_at.isoformat(),
                }
                for event in latest_order.events.all()[:8]
            ],
        }
    )
