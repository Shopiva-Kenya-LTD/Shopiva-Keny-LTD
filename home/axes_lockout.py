from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import render


def delivery_lockout(request, response, credentials, *args, **kwargs):
    """Keep Axes lockouts inside the branded delivery login page."""
    form = AuthenticationForm(request=request, data=request.POST or None)
    context = {
        "form": form,
        "lockout_message": "Too many unsuccessful login attempts. Please try again later.",
    }
    return render(request, "delivery/login.html", context, status=429)
