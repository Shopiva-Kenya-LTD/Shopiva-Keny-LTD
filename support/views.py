from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from home.models import Notification, SellerProfile

from .models import SupportMessage, SupportTicket

User = get_user_model()
MAX_MESSAGE_LENGTH = 8000
SYSTEM_SUBJECT_MARKERS = (
    "Payment assistance for order",
    "Order cancellation assistance",
    "Cancelled order assistance",
)


def _role_for(user):
    if user.is_staff or user.is_superuser:
        return "staff"
    if SellerProfile.objects.filter(user=user, is_active=True).exists():
        return "seller"
    return "customer"


def _is_system_ticket(ticket):
    return ticket.subject.startswith(SYSTEM_SUBJECT_MARKERS)


def _staff_notify(title, body, link):
    """Best-effort operational notification; never break a support action."""
    try:
        staff_users = User.objects.filter(is_staff=True, is_active=True)
        Notification.objects.bulk_create(
            [
                Notification(
                    user=staff_user,
                    notification_type="system",
                    title=title[:160],
                    message=body[:2000],
                    link=link[:255],
                )
                for staff_user in staff_users
            ]
        )
    except Exception:
        return


def _user_notify(user, title, body, link):
    """Best-effort customer/seller notification; never break a support action."""
    try:
        Notification.objects.create(
            user=user,
            notification_type="system",
            title=title[:160],
            message=body[:2000],
            link=link[:255],
        )
    except Exception:
        return


def _messages_payload(ticket):
    return [
        {
            "id": message.id,
            "body": message.body,
            "from_staff": message.from_staff,
            "author": "Shopiva Support" if message.from_staff else (message.author.get_full_name() or message.author.username if message.author else "You"),
            "created_at": message.created_at.isoformat(),
        }
        for message in ticket.messages.select_related("author").all()
    ]


def _valid_message(body):
    body = (body or "").strip()
    return body if body else None


@login_required(login_url="customer_login")
def support_center(request):
    role = _role_for(request.user)
    if role == "staff":
        return redirect("support_admin_center")

    role_label = "Seller" if role == "seller" else "Customer"
    back_url = "seller_dashboard" if role == "seller" else "customer_dashboard"
    tickets = SupportTicket.objects.filter(user=request.user).prefetch_related("messages__author")

    selected_id = request.GET.get("ticket") or request.POST.get("ticket_id")
    selected_ticket = None
    if selected_id:
        selected_ticket = get_object_or_404(tickets, id=selected_id)

    if request.method == "GET" and request.GET.get("format") == "json" and selected_ticket:
        return JsonResponse(
            {
                "ok": True,
                "status": selected_ticket.status,
                "status_label": selected_ticket.get_status_display(),
                "messages": _messages_payload(selected_ticket),
            }
        )

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "new":
            subject = request.POST.get("subject", "").strip()
            body = _valid_message(request.POST.get("body"))
            category = request.POST.get("category", "General").strip() or "General"
            priority = request.POST.get("priority", "normal")
            order_reference = request.POST.get("order_reference", "").strip()
            allowed_priorities = {choice[0] for choice in SupportTicket.PRIORITY_CHOICES}
            if not subject or len(subject) < 4 or not body:
                messages.error(request, "Please provide a subject and describe the issue so our support team can help.")
            elif priority not in allowed_priorities:
                messages.error(request, "Please select a valid support priority.")
            else:
                with transaction.atomic():
                    ticket = SupportTicket.objects.create(
                        user=request.user,
                        role=role,
                        subject=subject[:160],
                        category=category[:80],
                        priority=priority,
                        order_reference=order_reference[:120],
                        status="open",
                    )
                    SupportMessage.objects.create(ticket=ticket, author=request.user, body=body[:MAX_MESSAGE_LENGTH], from_staff=False)
                    transaction.on_commit(
                        lambda: _staff_notify(
                            f"New {role_label.lower()} support case",
                            f"{role_label} {request.user.username} opened '{ticket.subject}'.",
                            f"/admin/support-center/?ticket={ticket.id}",
                        )
                    )
                messages.success(request, f"Support case {str(ticket.id)[:8].upper()} opened. Our support desk has the case.")
                return redirect(f"/support/?ticket={ticket.id}")

        elif action == "reply" and selected_ticket:
            body = _valid_message(request.POST.get("body"))
            if selected_ticket.status == "closed":
                messages.error(request, "This support case is closed. Open a new case if you still need help.")
            elif body:
                SupportMessage.objects.create(ticket=selected_ticket, author=request.user, body=body[:MAX_MESSAGE_LENGTH], from_staff=False)
                selected_ticket.status = "open"
                selected_ticket.last_response_at = timezone.now()
                selected_ticket.save(update_fields=["status", "last_response_at", "updated_at"])
                _staff_notify(
                    "Customer replied to support case" if role == "customer" else "Seller replied to support case",
                    f"{role_label} {request.user.username} replied to '{selected_ticket.subject}'.",
                    f"/admin/support-center/?ticket={selected_ticket.id}",
                )
                messages.success(request, "Your reply has been sent to Shopiva Support.")
            return redirect(f"/support/?ticket={selected_ticket.id}")

    if selected_ticket is None:
        selected_ticket = tickets.first()

    open_user_count = tickets.filter(status__in=("open", "in_progress", "waiting_for_customer")).count()
    system_count = sum(1 for ticket in tickets if _is_system_ticket(ticket))

    return render(
        request,
        "support/center.html",
        {
            "tickets": tickets,
            "selected_ticket": selected_ticket,
            "role": role,
            "role_label": role_label,
            "back_url": back_url,
            "is_empty": not tickets.exists(),
            "open_user_count": open_user_count,
            "system_count": system_count,
            "customer_raised_count": customer_count,
            "seller_raised_count": seller_count,
            "system_raised_count": system_count,
            "urgent_issues_count": urgent_count,
        },
    )


@staff_member_required(login_url="admin_login")
def support_admin_center(request):
    tickets = SupportTicket.objects.select_related("user").prefetch_related("messages__author").all()

    status_filter = request.GET.get("status", "").strip()
    role_filter = request.GET.get("role", "").strip()
    priority_filter = request.GET.get("priority", "").strip()
    source_filter = request.GET.get("source", "").strip()
    search = request.GET.get("q", "").strip()
    active_statuses = ("open", "in_progress", "waiting_for_customer")
    if status_filter and status_filter != "all":
        tickets = tickets.filter(status=status_filter)
    elif status_filter != "all":
        # The four operational queues show active work only. Resolved/closed
        # cases remain available through the explicit history filters.
        tickets = tickets.filter(status__in=active_statuses)
    if role_filter:
        tickets = tickets.filter(role=role_filter)
    if priority_filter:
        tickets = tickets.filter(priority=priority_filter)
    if source_filter == "system":
        tickets = tickets.filter(
            Q(subject__startswith="Payment assistance for order")
            | Q(subject__startswith="Order cancellation assistance")
            | Q(subject__startswith="Cancelled order assistance")
        )
    elif source_filter == "user":
        tickets = tickets.exclude(
            Q(subject__startswith="Payment assistance for order")
            | Q(subject__startswith="Order cancellation assistance")
            | Q(subject__startswith="Cancelled order assistance")
        )
    if search:
        tickets = tickets.filter(Q(subject__icontains=search) | Q(category__icontains=search) | Q(order_reference__icontains=search) | Q(user__username__icontains=search) | Q(user__email__icontains=search))

    selected_id = request.GET.get("ticket") or request.POST.get("ticket_id")
    selected_ticket = get_object_or_404(SupportTicket.objects.select_related("user").prefetch_related("messages__author"), id=selected_id) if selected_id else tickets.first()

    if request.method == "GET" and request.GET.get("format") == "json" and selected_ticket:
        return JsonResponse(
            {
                "ok": True,
                "status": selected_ticket.status,
                "status_label": selected_ticket.get_status_display(),
                "messages": _messages_payload(selected_ticket),
            }
        )

    if request.method == "POST" and selected_ticket:
        action = request.POST.get("action", "")
        if action == "reply":
            body = _valid_message(request.POST.get("body"))
            if selected_ticket.status == "closed":
                messages.error(request, "Closed support cases cannot receive new replies. Reopen the case first.")
            elif body:
                SupportMessage.objects.create(ticket=selected_ticket, author=request.user, body=body[:MAX_MESSAGE_LENGTH], from_staff=True)
                selected_ticket.status = "waiting_for_customer"
                selected_ticket.last_response_at = timezone.now()
                selected_ticket.save(update_fields=["status", "last_response_at", "updated_at"])
                _user_notify(
                    selected_ticket.user,
                    "Shopiva Support replied",
                    f"Support has replied to your case: {selected_ticket.subject}.",
                    f"/support/?ticket={selected_ticket.id}",
                )
                messages.success(request, "Reply sent to the customer/seller.")
        elif action == "status":
            new_status = request.POST.get("status", "")
            allowed = {choice[0] for choice in SupportTicket.STATUS_CHOICES}
            if new_status in allowed:
                selected_ticket.status = new_status
                selected_ticket.last_response_at = timezone.now()
                selected_ticket.save(update_fields=["status", "last_response_at", "updated_at"])
                _user_notify(
                    selected_ticket.user,
                    f"Support case {selected_ticket.get_status_display().lower()}",
                    f"Your Shopiva support case '{selected_ticket.subject}' is now {selected_ticket.get_status_display().lower()}.",
                    f"/support/?ticket={selected_ticket.id}",
                )
                messages.success(request, "Support case status updated.")
                if new_status in {"resolved", "closed"}:
                    # Resolution immediately removes the case from operational
                    # queues while preserving it for support history/audit.
                    return redirect(
                        f"/admin/support-center/?role={role_filter}&priority={priority_filter}"
                        f"&source={source_filter}&q={search}"
                    )
        elif action == "delete":
            # Permanent deletion is deliberately restricted to resolved/closed
            # cases so active support work cannot be removed accidentally.
            if not (_is_system_ticket(selected_ticket) or selected_ticket.status in {"resolved", "closed"}):
                messages.error(request, "Only system-raised or resolved/closed cases can be deleted.")
            else:
                case_id = str(selected_ticket.id)[:8].upper()
                selected_ticket.delete()
                messages.success(request, f"Support case {case_id} was permanently deleted.")
                return redirect(
                    f"/admin/support-center/?role={role_filter}&priority={priority_filter}"
                    f"&source={source_filter}&q={search}&status={status_filter}"
                )
        return redirect(f"/admin/support-center/?ticket={selected_ticket.id}")

    open_count = SupportTicket.objects.filter(status__in=active_statuses).count()
    urgent_count = SupportTicket.objects.filter(priority="urgent", status__in=active_statuses).count()
    system_ticket_q = (
        Q(subject__startswith="Payment assistance for order")
        | Q(subject__startswith="Order cancellation assistance")
        | Q(subject__startswith="Cancelled order assistance")
    )
    system_count = SupportTicket.objects.filter(system_ticket_q, status__in=active_statuses).count()
    # Customer/Seller queue counts represent user-raised support cases only.
    # System-generated cases must not inflate customer or seller activity counts.
    seller_count = SupportTicket.objects.filter(
        role="seller",
        status__in=active_statuses,
    ).exclude(system_ticket_q).count()
    customer_count = SupportTicket.objects.filter(
        role="customer",
        status__in=active_statuses,
    ).exclude(system_ticket_q).count()

    return render(
        request,
        "support/admin_center.html",
        {
            "tickets": tickets,
            "selected_ticket": selected_ticket,
            "open_count": open_count,
            "urgent_count": urgent_count,
            "seller_count": seller_count,
            "customer_count": customer_count,
            "system_count": system_count,
            "status_choices": SupportTicket.STATUS_CHOICES,
            "role_choices": SupportTicket.ROLE_CHOICES,
            "priority_choices": SupportTicket.PRIORITY_CHOICES,
            "current_status": status_filter,
            "current_role": role_filter,
            "current_priority": priority_filter,
            "current_source": source_filter,
            "query": search,
        },
    )
