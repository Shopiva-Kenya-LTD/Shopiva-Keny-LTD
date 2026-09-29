from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect, render

from .models import Notification


def _role_for(user):
    if user.is_staff or user.is_superuser:
        return "admin"
    seller = getattr(user, "seller_profile", None)
    if seller and seller.is_active:
        return "seller"
    return "customer"


def notification_center(request):
    if not request.user.is_authenticated:
        return redirect("customer_login")

    role = _role_for(request.user)
    if role == "customer" and not (not request.user.is_staff and not request.user.is_superuser):
        return redirect("customer_login")

    if role == "seller":
        back_url = "seller_dashboard"
    elif role == "admin":
        back_url = "/admin/"
    else:
        back_url = "customer_dashboard"

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "read_all":
            request.user.shopiva_notifications.filter(is_read=False).update(is_read=True)
            messages.success(request, "All notifications marked as read.")
        elif action == "read":
            notification_id = request.POST.get("notification_id")
            if notification_id:
                Notification.objects.filter(id=notification_id, user=request.user, is_read=False).update(is_read=True)
        elif action == "delete" and role == "seller":
            notification_id = request.POST.get("notification_id")
            if notification_id:
                Notification.objects.filter(id=notification_id, user=request.user).delete()
                messages.success(request, "Notification deleted.")
        elif action == "delete_selected" and role == "seller":
            notification_ids = request.POST.getlist("notification_ids")
            if notification_ids:
                deleted_count, _ = Notification.objects.filter(
                    id__in=notification_ids,
                    user=request.user,
                ).delete()
                messages.success(request, f"{deleted_count} notification(s) deleted.")
            else:
                messages.info(request, "Select at least one notification to delete.")
        elif action == "delete_all" and role == "seller":
            deleted_count, _ = Notification.objects.filter(user=request.user).delete()
            messages.success(request, f"{deleted_count} notification(s) deleted.")
        return redirect(request.path)

    notifications = request.user.shopiva_notifications.all().order_by("-created_at")[:50]
    unread_count = request.user.shopiva_notifications.filter(is_read=False).count()
    total_count = request.user.shopiva_notifications.count()

    return render(
        request,
        "accounts/notifications.html",
        {
            "notifications": notifications,
            "unread_count": unread_count,
            "total_count": total_count,
            "role": role,
            "back_url": back_url,
        },
    )


@login_required(login_url="customer_login")
def customer_notification_center(request):
    if request.user.is_staff or request.user.is_superuser or getattr(request.user, "seller_profile", None):
        return redirect("notification_center")
    return notification_center(request)


@login_required(login_url="seller_login")
def seller_notification_center(request):
    seller = getattr(request.user, "seller_profile", None)
    if request.user.is_staff or request.user.is_superuser or not seller or not seller.is_active:
        return redirect("seller_login")
    return notification_center(request)


@login_required(login_url="admin_login")
def admin_notification_center(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("admin_login")

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "read_all":
            request.user.shopiva_notifications.filter(is_read=False).update(is_read=True)
            messages.success(request, "All notifications marked as read.")
        elif action == "read":
            notification_id = request.POST.get("notification_id")
            if notification_id:
                Notification.objects.filter(id=notification_id, user=request.user, is_read=False).update(is_read=True)
        return redirect(request.path)

    notifications = request.user.shopiva_notifications.all().order_by("-created_at")[:50]
    unread_count = request.user.shopiva_notifications.filter(is_read=False).count()
    total_count = request.user.shopiva_notifications.count()
    return render(
        request,
        "admin/notification_center.html",
        {"notifications": notifications, "unread_count": unread_count, "total_count": total_count, "role": "admin"},
    )
