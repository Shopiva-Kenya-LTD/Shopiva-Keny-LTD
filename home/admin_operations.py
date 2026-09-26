from django.contrib import messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import DeliveryAgent, Notification, Order, PaymentTransaction, Product
from .notification_service import notify_user
from support.models import SupportTicket


SYSTEM_TICKET_Q = (
    Q(subject__startswith="Payment assistance for order")
    | Q(subject__startswith="Order cancellation assistance")
    | Q(subject__startswith="Cancelled order assistance")
)


@staff_member_required(login_url="admin_login")
def admin_operations_center(request):
    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        agent_id = request.POST.get("agent_id", "").strip()
        verification_confirmed = request.POST.get("verification_confirmed") == "1"

        if action in {"approve_delivery", "deactivate_delivery"} and agent_id.isdigit():
            agent = (
                DeliveryAgent.objects.select_related("user")
                .filter(id=int(agent_id))
                .first()
            )
            if not agent:
                messages.error(request, "Delivery partner was not found.")
                return redirect("shopiva_admin:approval_center")

            if action == "approve_delivery" and not verification_confirmed:
                messages.error(
                    request,
                    "Verification is required before approving a new staff member. "
                    "Confirm that the applicant's identity and submitted vehicle details have been checked.",
                )
                return redirect("shopiva_admin:approval_center")

            with transaction.atomic():
                enabled = action == "approve_delivery"
                agent.is_active = enabled
                if not enabled:
                    agent.status = "offline"
                agent.save(update_fields=["is_active", "status"])

                agent.user.is_active = enabled
                agent.user.save(update_fields=["is_active"])

                LogEntry.objects.log_actions(
                    user_id=request.user.pk,
                    queryset=[agent],
                    action_flag=CHANGE,
                    change_message=(
                        "Staff application verified and approved by admin."
                        if enabled
                        else "Staff application kept inactive/deactivated by admin."
                    ),
                    single_object=True,
                )

                if enabled:
                    messages.success(
                        request,
                        f"{agent.display_name} has been verified and approved for delivery operations.",
                    )
                else:
                    messages.info(
                        request,
                        f"{agent.display_name} remains inactive and cannot access delivery operations.",
                    )

            # Confirm the outcome to the applicant. This creates an in-app
            # notification immediately and uses any configured external channels.
            if enabled:
                notify_user(
                    agent.user,
                    "system",
                    "Delivery staff application approved",
                    (
                        "Your Shopiva delivery staff application has been verified and approved. "
                        "You can now sign in to the Shopiva Delivery app."
                    ),
                    link="/delivery/login/",
                    email=agent.user.email,
                    phone=agent.phone,
                )
            else:
                notify_user(
                    agent.user,
                    "system",
                    "Delivery staff application not approved",
                    (
                        "Your Shopiva delivery staff application is still inactive. "
                        "Please contact Shopiva administration if you need clarification."
                    ),
                    link="/delivery/login/",
                    email=agent.user.email,
                    phone=agent.phone,
                )

        return redirect("shopiva_admin:approval_center")

    active_statuses = ("open", "in_progress", "waiting_for_customer")
    pending_delivery_qs = DeliveryAgent.objects.filter(is_active=False).select_related("user").order_by("-created_at")
    pending_delivery = pending_delivery_qs[:50]
    approved_delivery_count = DeliveryAgent.objects.filter(is_active=True).count()
    user_issues = SupportTicket.objects.exclude(SYSTEM_TICKET_Q).filter(
        status__in=active_statuses
    ).select_related("user").order_by("-updated_at")[:50]
    system_issues = SupportTicket.objects.filter(
        SYSTEM_TICKET_Q, status__in=active_statuses
    ).select_related("user").order_by("-updated_at")[:50]
    notifications = (
        Notification.objects.filter(user__is_staff=True)
        .select_related("user")
        .order_by("-created_at")[:50]
    )

    failed_payments = PaymentTransaction.objects.filter(
        status__in=("failed", "cancelled", "refunded")
    ).select_related("order").order_by("-updated_at")[:25]
    unassigned_orders = Order.objects.filter(
        delivery_agent__isnull=True,
        status__in=("confirmed", "paid", "packed", "processing", "shipped"),
    ).order_by("-created_at")[:25]
    low_stock = Product.objects.filter(
        is_active=True, stock_quantity__lte=5
    ).order_by("stock_quantity", "name")[:25]

    now = timezone.now()
    stale_riders = []
    for rider in DeliveryAgent.objects.filter(is_active=True).select_related("user"):
        if rider.last_location_at and (now - rider.last_location_at).total_seconds() > 90:
            stale_riders.append(rider)
        elif rider.status in {"available", "on_delivery"} and not rider.last_location_at:
            stale_riders.append(rider)

    return render(
        request,
        "admin/operations_center.html",
        {
            "pending_delivery": pending_delivery,
            "user_issues": user_issues,
            "system_issues": system_issues,
            "notifications": notifications,
            "failed_payments": failed_payments,
            "unassigned_orders": unassigned_orders,
            "low_stock": low_stock,
            "stale_riders": stale_riders[:25],
            "counts": {
                "pending_delivery": pending_delivery_qs.count(),
                "approved_delivery": approved_delivery_count,
                "user_issues": SupportTicket.objects.exclude(SYSTEM_TICKET_Q).filter(status__in=active_statuses).count(),
                "system_issues": SupportTicket.objects.filter(SYSTEM_TICKET_Q, status__in=active_statuses).count(),
                "unread_notifications": Notification.objects.filter(user__is_staff=True, is_read=False).count(),
                "failed_payments": PaymentTransaction.objects.filter(status__in=("failed", "cancelled", "refunded")).count(),
                "unassigned_orders": Order.objects.filter(delivery_agent__isnull=True, status__in=("confirmed", "paid", "packed", "processing", "shipped")).count(),
                "low_stock": Product.objects.filter(is_active=True, stock_quantity__lte=5).count(),
                "stale_riders": len(stale_riders),
            },
        },
    )
