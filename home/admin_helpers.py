from decimal import Decimal

from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.db.models import Sum
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

from .models import DeliveryAgent, Order, PaymentTransaction, Product
from .nia_admin import answer_admin_question
from .nia_core import call_nia


def _require_admin(request):
    return bool(request.user.is_authenticated and request.user.is_staff)


def _no_store(response):
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@never_cache
def admin_login(request):
    """Dedicated Shopiva admin login using Django's validated authentication form."""
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("shopiva_admin:index")

    form = AuthenticationForm(request, data=request.POST or None)
    next_url = request.POST.get("next") or request.GET.get("next") or ""

    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        if not user.is_active:
            form.add_error(None, "This administrator account is inactive.")
        elif not user.is_staff:
            form.add_error(None, "This account is not authorized for the Shopiva Control Center.")
        else:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return HttpResponseRedirect(next_url)
            return redirect("shopiva_admin:index")

    context = {
        "form": form,
        "app_path": request.path,
        "next": next_url,
        "site_header": "Shopiva Control Center",
        "site_title": "Shopiva Admin",
    }
    return render(request, "admin/login.html", context)


@never_cache
def admin_logout(request):
    """Destroy the admin session and explicitly prevent cached authenticated pages."""
    if request.method != "POST":
        return _no_store(JsonResponse({"ok": False, "error": "Method not allowed."}, status=405))
    if not _require_admin(request):
        return _no_store(HttpResponseRedirect(reverse("shopiva_admin:login")))
    logout(request)
    return _no_store(HttpResponseRedirect(reverse("shopiva_admin:login")))


