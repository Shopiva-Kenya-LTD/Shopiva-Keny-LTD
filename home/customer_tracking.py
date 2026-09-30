from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .delivery_payouts import queue_delivery_payout, record_delivery_earning, initiate_delivery_payout

from .models import Order


@login_required(login_url="customer_login")
def customer_order_tracking(request, order_id):
    if request.user.is_staff or request.user.is_superuser:
        raise Http404

    order = get_object_or_404(
        Order.objects.select_related("delivery_agent").prefetch_related("items__product", "events"),
        id=order_id,
        customer=request.user,
    )

    if request.method == "POST":
        if request.POST.get("action") != "confirm_received":
            return redirect("customer_order_tracking", order_id=order.id)
        if order.status != "delivered":
            messages.error(request, "The delivery must be marked delivered before you can confirm receipt.")
            return redirect("customer_order_tracking", order_id=order.id)
        if order.customer_delivery_confirmed:
            messages.info(request, "You have already confirmed receipt of this order.")
            return redirect("customer_order_tracking", order_id=order.id)
        if not order.delivery_agent:
            messages.error(request, "No delivery partner is attached to this order.")
            return redirect("customer_order_tracking", order_id=order.id)

        with transaction.atomic():
            locked = Order.objects.select_for_update().select_related("delivery_agent").get(
                pk=order.pk,
                customer=request.user,
            )
            if not locked.customer_delivery_confirmed:
                locked.customer_delivery_confirmed = True
                locked.customer_delivery_confirmed_at = timezone.now()
                locked.customer_delivery_confirmed_by = request.user
                locked.save(update_fields=(
                    "customer_delivery_confirmed",
                    "customer_delivery_confirmed_at",
                    "customer_delivery_confirmed_by",
                ))
                earning = record_delivery_earning(locked, locked.delivery_agent)
                payout = None
                if earning:
                    try:
                        payout = queue_delivery_payout(locked.delivery_agent, automatic=True)
                    except ValueError:
                        payout = None
                    if payout:
                        payout = initiate_delivery_payout(payout)

        messages.success(request, "Receipt confirmed. The rider commission has been credited and any eligible automatic payout has been queued.")
        return redirect("customer_order_tracking", order_id=order.id)

    return render(
        request,
        "accounts/order_tracking.html",
        {
            "order": order,
            "events": order.events.all(),
        },
    )
