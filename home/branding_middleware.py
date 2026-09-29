import re
from django.http import HttpResponse


def _strip_brand_images(html):
    """Remove legacy logo <img> and visual fallback elements from the header brand block."""
    tag_rx = re.compile(r"<([A-Za-z][\\w:-]*)\\b[^>]*>", re.IGNORECASE)
    class_rx = re.compile(r"class\\s*=\\s*['\"]([^'\"]*)['\"]", re.IGNORECASE)
    opening = None
    tag_name = None
    for match in tag_rx.finditer(html):
        classes = class_rx.search(match.group(0))
        if classes and re.search(r"\\bbrand\\b", classes.group(1), re.IGNORECASE):
            opening = match
            tag_name = match.group(1).lower()
            break
    if not opening:
        return html, False

    token_rx = re.compile(r"<(/?)([A-Za-z][\\w:-]*)\\b[^>]*>", re.IGNORECASE)
    depth = 1
    end = None
    closing = ""
    for token in token_rx.finditer(html, opening.end()):
        if token.group(2).lower() != tag_name:
            continue
        if token.group(1):
            depth -= 1
            if depth == 0:
                end = token.end()
                closing = token.group(0)
                break
        elif not token.group(0).rstrip().endswith("/>"):
            depth += 1
    if end is None:
        return html, False

    block_end = end - len(closing)
    block = html[opening.end():block_end]
    cleaned = re.sub(r"(?is)<img\\b[^>]*>\\s*", "", block)
    cleaned = re.sub(r"(?is)<[^>]*\\bbrand-fallback\\b[^>]*>.*?</[^>]+>\\s*", "", cleaned)
    if cleaned == block:
        return html, False
    return html[:opening.end()] + cleaned + html[block_end:], True


def _support_widget(role):
    label = "Seller" if role == "seller" else "Customer"
    return f'''\n<!-- Shopiva 24/7 support launcher: injected at the dashboard shell so it survives dashboard redesigns. -->\n<style id="shopiva-support-widget-style">\n#shopiva-support-launcher{{position:fixed;right:20px;bottom:20px;z-index:99990;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}\n#shopiva-support-launcher button{{border:0;border-radius:16px;background:linear-gradient(135deg,#087f5b,#07543f);color:#fff;padding:13px 16px;box-shadow:0 14px 36px rgba(3,56,42,.28);font-weight:950;font-size:12px;cursor:pointer}}\n#shopiva-support-panel{{display:none;width:min(330px,calc(100vw - 28px));margin-bottom:10px;background:#fff;border:1px solid #dbe8e2;border-radius:18px;box-shadow:0 20px 55px rgba(13,35,28,.18);overflow:hidden}}\n#shopiva-support-panel.open{{display:block}}\n.ssp-head{{padding:15px 16px;background:linear-gradient(135deg,#06261f,#0a4a3a);color:#fff}}\n.ssp-head b{{display:block;font-size:14px}}.ssp-head span{{display:block;margin-top:4px;color:#c9e9df;font-size:9px}}\n.ssp-body{{padding:12px}}.ssp-card{{display:block;text-decoration:none;color:#17221e;border:1px solid #e2ebe6;border-radius:12px;padding:11px;margin-bottom:8px;background:#fbfdfc}}\n.ssp-card b{{font-size:11px}}.ssp-card span{{display:block;color:#718078;font-size:9px;margin-top:3px;line-height:1.4}}\n.ssp-live{{display:flex;align-items:center;gap:6px;color:#08784f;font-size:9px;font-weight:900;margin:3px 2px 10px}}.ssp-dot{{width:7px;height:7px;border-radius:50%;background:#11b86b}}\n@media(max-width:560px){{#shopiva-support-launcher{{right:12px;bottom:12px}}#shopiva-support-launcher button{{padding:12px 14px}}}}\n</style>\n<div id="shopiva-support-launcher" data-role="{label}">\n  <div id="shopiva-support-panel" aria-hidden="true">\n    <div class="ssp-head"><b>Shopiva Support 24/7</b><span>{label} assistance · cases stay linked to your account</span></div>\n    <div class="ssp-body">\n      <div class="ssp-live"><i class="ssp-dot"></i> Digital support is online</div>\n      <a class="ssp-card" href="/support/"><b>💬 Support & Chat Center</b><span>Open a case, reply to support and follow your conversation.</span></a>\n      \n      <a class="ssp-card" href="/support/?new=1"><b>🚨 Report an Issue</b><span>Report payment, order, delivery, account, product or seller problems.</span></a>\n    </div>\n  </div>\n  <button type="button" id="shopiva-support-toggle" aria-expanded="false">💬 Support 24/7</button>\n</div>\n<script>(function(){{var b=document.getElementById('shopiva-support-toggle'),p=document.getElementById('shopiva-support-panel');if(!b||!p)return;b.addEventListener('click',function(){{var open=p.classList.toggle('open');p.setAttribute('aria-hidden',String(!open));b.setAttribute('aria-expanded',String(open));b.textContent=open?'✕ Close Support':'💬 Support 24/7'}});}})();</script>\n'''


class ShopivaBrandingMiddleware:
    """Final public-home cleanup plus a resilient support launcher on customer/seller dashboards."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type or not response.content:
            return response

        html = response.content.decode("utf-8", errors="replace")
        changed = False

        if request.path == "/":
            html, cleaned = _strip_brand_images(html)
            changed = changed or cleaned

        is_customer_dashboard = request.path == "/account/"
        is_seller_dashboard = request.path == "/seller/"
        user = getattr(request, "user", None)
        if (is_customer_dashboard or is_seller_dashboard) and user and user.is_authenticated and not user.is_staff and not user.is_superuser:
            role = "seller" if is_seller_dashboard else "customer"
            if "id=\"shopiva-support-launcher\"" not in html:
                html = html.replace("</body>", _support_widget(role) + "</body>", 1)
                changed = True

        if not changed:
            return response

        new_response = HttpResponse(
            html,
            status=response.status_code,
            content_type=response.get("Content-Type", "text/html"),
        )
        response_headers = ("Cache-Control", "Pragma", "Expires", "X-Shopiva-Brand-Cleanup")
        for header in response_headers:
            if header in response:
                new_response[header] = response[header]
        if request.path == "/":
            new_response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            new_response["Pragma"] = "no-cache"
            new_response["Expires"] = "0"
            new_response["X-Shopiva-Brand-Cleanup"] = "header-image-free-v1"
        new_response["X-Shopiva-Support-Launcher"] = "24-7-dashboard-v1" if (is_customer_dashboard or is_seller_dashboard) else "inactive"
        return new_response
