from functools import wraps

from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import redirect, render


def seller_login(request):
    """Authenticate only a dedicated, active seller account.

    Shopiva deliberately keeps customer, seller, delivery and admin sessions
    on separate portals. An already-authenticated customer cannot silently
    switch identities by submitting seller credentials in the same session.
    """
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            return redirect("/admin/")
        seller = getattr(request.user, "seller_profile", None)
        if seller:
            if seller.is_active:
                return redirect("seller_dashboard")
            auth_logout(request)
        else:
            messages.info(
                request,
                "You are signed in as a customer. Sign out first, then use a separate Shopiva seller account.",
            )
            return redirect("customer_dashboard")

    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if user.is_staff or user.is_superuser:
                form.add_error(None, "Admin accounts must use the Shopiva Admin Control Center.")
                return render(request, "seller/login.html", {"form": form})
            seller = getattr(user, "seller_profile", None)
            if not seller or not seller.is_active:
                form.add_error(
                    None,
                    "This username belongs to a customer account, or the seller account is inactive. "
                    "Use Seller Sign Up to create a dedicated seller account.",
                )
            elif hasattr(user, "delivery_agent_profile"):
                form.add_error(None, "Delivery accounts must use the Delivery Portal.")
            else:
                auth_login(request, user)
                messages.success(request, f"Welcome back, {seller.business_name or user.username}!")
                return redirect("seller_dashboard")
    else:
        form = AuthenticationForm(request)

    return render(request, "seller/login.html", {"form": form})


def seller_logout(request):
    was_admin = bool(
        request.user.is_authenticated
        and (request.user.is_staff or request.user.is_superuser)
    )
    auth_logout(request)
    if was_admin:
        return redirect("admin_login")
    messages.success(request, "You have been signed out of the seller portal.")
    return redirect("seller_login")


def seller_login_required(view_func):
    """Require an authenticated, active Shopiva seller for seller-only pages."""
    @wraps(view_func)
    @login_required(login_url="seller_login")
    def wrapped(request, *args, **kwargs):
        if request.user.is_staff or request.user.is_superuser:
            return redirect("/admin/")
        seller = getattr(request.user, "seller_profile", None)
        if not seller or not seller.is_active:
            messages.error(
                request,
                "This is not an active seller session. Customer and seller accounts are kept separate.",
            )
            return redirect("seller_login")
        if hasattr(request.user, "delivery_agent_profile"):
            messages.error(request, "Delivery accounts cannot access the seller workspace.")
            return redirect("seller_login")
        return view_func(request, *args, **kwargs)

    return wrapped
