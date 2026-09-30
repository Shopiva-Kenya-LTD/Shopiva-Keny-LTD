from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .delivery_payouts import record_delivery_earning

from .models import Order, PaymentTransaction


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
                payment = PaymentTransaction.objects.select_for_update().filter(order=locked).order_by("-created_at").first()
                if payment and payment.method == "cod" and payment.status == "pending":
                    payment.status = "paid"
                    payment.provider_reference = payment.provider_reference or f"COD-RECEIVED-{locked.id}"
                    payment.paid_at = timezone.now()
                    payment.save(update_fields=("status", "provider_reference", "paid_at", "updated_at"))
                    locked.payment_status = "paid"
                    locked.payment_reference = payment.provider_reference
                    locked.paid_at = timezone.now()
                    locked.status = "delivered"
                    locked.save(update_fields=("payment_status", "payment_reference", "paid_at", "status"))
                    
                if locked.payment_status != "paid":
                    messages.error(request, "Payment has not been confirmed yet. The rider commission cannot be released.")
                    return redirect("customer_order_tracking", order_id=order.id)

                locked.customer_delivery_confirmed = True
                locked.customer_delivery_confirmed_at = timezone.now()
                locked.customer_delivery_confirmed_by = request.user
                locked.save(update_fields=(
                    "customer_delivery_confirmed",
                    "customer_delivery_confirmed_at",
                    "customer_delivery_confirmed_by",
                ))
                record_delivery_earning(locked, locked.delivery_agent)

        messages.success(request, "Receipt confirmed. The rider commission has been credited. The rider can now request payout, which requires admin payment confirmation.")
        return redirect("customer_order_tracking", order_id=order.id)

    return render(
        request,
        "accounts/order_tracking.html",
        {
            "order": order,
            "events": order.events.all(),
        },
    )
