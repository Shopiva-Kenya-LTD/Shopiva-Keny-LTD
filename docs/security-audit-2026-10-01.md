# Production security audit and release hardening — 2026-10-01

## Scope and method

This repository audit covered Django configuration, all URL routes and custom
admin routes, authentication boundaries, CSRF-exempt endpoints, payment and
payout state changes, uploads, deployment configuration, templates, and
management commands. It is a point-in-time code audit; it cannot attest to
Render, DNS, Safaricom, Cloudinary, Twilio, Google, database, or OpenAI
configuration that is not present in this repository.

No production credential was rotated, replaced, generated, or committed.

## Findings

| Severity | File / function | Vulnerability and impact | Fix / verification |
| --- | --- | --- | --- |
| HIGH | `home/admin_helpers.py` / `admin_logout` | The administrator logout endpoint was CSRF-exempt and accepted GET, allowing a third party to force an administrator logout. | It is now POST-only and protected by Django CSRF middleware. `ReleaseSecurityHardeningTests.test_admin_logout_requires_post_and_csrf` verifies both controls. |
| HIGH | `home/views.py` / `seller_request_payout` | Available seller funds were checked without a locked reservation. Concurrent requests could queue more than the available balance; a request also did not deduct its reserved balance. | The wallet is row-locked, an open request is rejected, balance is reserved atomically, and M-PESA destination format is validated. Test verifies reservation and duplicate rejection. |
| HIGH | `home/admin.py` / `SellerPayoutRequestAdmin.save_model` | A terminal seller payout could be changed again through the Django admin state field, risking an incorrect financial record or duplicate manual payment workflow. | Explicit one-way transition rules and a payment-reference requirement now apply to administrative status changes. |
| MEDIUM | `home/forms.py` / product upload fields | File inputs accepted arbitrary file content; downstream image processing could receive malformed, oversized, or decompression-bomb images. | JPEG/PNG/WebP content verification, 10 MB limit, and 6000px dimension ceiling are enforced before Cloudinary upload. |
| MEDIUM | `shopiva/urls.py` | Django development media serving was enabled in production, potentially exposing local media storage through the application process. | Media URL patterns are registered only when `DEBUG=True`; production uses the configured media provider. |
| MEDIUM | `shopiva/settings.py` / template paths | Project-level `templates/admin/products/*` preceded `home/templates/admin/products/*`, so legacy product templates could shadow the Product Creation Studio. | `home/templates` is first in deterministic template search order. The legacy URLs still route to the same protected current view. |
| MEDIUM | `shopiva/settings.py` / HSTS | `includeSubDomains` and preload were enabled in code without proof that every current/future subdomain has valid HTTPS. A bad subdomain could become unavailable for returning users. | Preload is disabled; include-subdomains is opt-in via a reviewed environment variable. HSTS remains enabled for the configured production host. |
| LOW | `home/security_middleware.py` | Permissions Policy was not set. | A restrictive policy disables unused browser capabilities while retaining geolocation/microphone required by mapped delivery and Nia flows. |
| INFORMATIONAL | `shopiva/settings.py` / CSP | CSP is report-only because the current application relies on inline scripts and multiple third-party map/media sources. Enforcing it now without reporting would risk checkout and operations outages. | Existing report-only policy is retained. See external configuration checklist for a staged nonce/hash-based rollout. |
| HIGH (remaining) | `home/payments.py` / `mpesa_callback` | Daraja STK callback handling performs receipt, amount, and phone validation plus row locking/idempotency, but the public callback has no repository-configured provider signature or verified source-network control. A party able to learn a checkout ID could attempt a forged callback. | Do **not** invent a secret or IP range. Confirm Safaricom-supported authentication/source validation and configure it before production sign-off. The code must then validate that mechanism. |

## Controls reviewed

* **Authentication, roles, IDOR:** customer order tracking scopes orders to the customer; seller product/order routes scope objects to seller; delivery actions scope orders to the assigned active agent; custom admin routes use Django `admin_view` or staff decorators. Login uses Django password hashing/validators, session rotation through `login()`, Axes lockout, and HTTPS-only/HttpOnly/Lax session cookies in production.
* **Requests and browser security:** POST actions rely on `CsrfViewMiddleware` unless machine-to-machine. CSRF-exempt endpoints identified were M-PESA callback, Stripe webhook, Twilio/Nia voice callbacks, and protected Nia task runner. Stripe verifies its signature; Nia task runner requires `NIA_CRON_SECRET`; M-PESA and Twilio need the external configuration review below. X-Frame-Options, nosniff, referrer policy, COOP/CORP, HSTS, SSL redirect behind Render proxy, report-only CSP, and Permissions Policy are configured.
* **Payments and races:** checkout calculates totals server-side, locks inventory, and creates payment idempotency keys. M-PESA callback locks payment/order rows, validates receipt/amount/phone, avoids duplicate paid transitions, and settlement creation uses a unique order/seller constraint. Delivery earnings require delivered status, customer receipt confirmation, and paid order status; delivery payouts reserve locked ledger rows and remain manual/admin-recorded (no B2C/PesaLink path). Seller payout reservation is hardened in this release.
* **Uploads, XSS, injection, redirects:** ORM query construction was used in reviewed application code; redirect validation is present for admin login; product uploads now validate actual image content. Django autoescaping is used by the reviewed templates. No command execution endpoint was identified. External URL use (maps/provider APIs) remains allowlisted/provider-fixed in reviewed code.
* **Operations:** `DEBUG` defaults false, `SECRET_KEY` is required outside debug, allowed hosts/trusted origins are environment-controlled, errors are normally logged server-side, Render runs checks/migrations/preflight/collectstatic, and `makemigrations --check --dry-run` was added before migration.

## External configuration required before release

1. Confirm the live Render service name/configuration and its deployed Git commit against the audited commit after CI succeeds. This audit cannot access Render’s service dashboard or deploy.
2. Keep `DEBUG=false`; set production-only `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `PUBLIC_SITE_URL`, `DATABASE_URL`, and a high-entropy `SECRET_KEY` in Render’s secret store.
3. Confirm Render terminates TLS and forwards `X-Forwarded-Proto: https`; leave `SECURE_SSL_REDIRECT=true` only after this verification.
4. For M-PESA, confirm the approved callback URL, merchant profile, Till/shortcode, passkey, and the strongest Safaricom-supported callback authentication/source-validation mechanism. Configure any validation secret or source list only from Safaricom documentation/portal; do not guess it.
5. Configure `STRIPE_WEBHOOK_SECRET` only if Stripe remains enabled. Disable/remove the provider route only through a separately reviewed product decision.
6. Verify Twilio request-signature validation for every inbound voice/SMS webhook, including the exact public URL/proxy scheme. Configure Twilio credentials and any Nia caller-verification settings only in Render secrets.
7. Verify Cloudinary uses a restricted production account/preset, allows image resources only, and does not expose an unsigned upload preset that can write unrestricted assets. Keep `CLOUDINARY_API_SECRET` server-only.
8. Restrict Google Maps browser key by production origin and APIs; restrict server keys by service/IP only after verified provider guidance. Restrict OpenAI, Africa’s Talking, WhatsApp, Resend, Co-op Connect, and any Nia provider credentials to their minimum permissions.
9. Review database TLS, least-privilege database user, backups/restore test, retention, and Render log access. No database operation is performed by this audit.
10. Set `SECURE_HSTS_INCLUDE_SUBDOMAINS=true` only after every subdomain is inventoried and HTTPS-protected. Do not enable preload without the separate preload eligibility audit.

## Environment variables requiring real values (never invent or rotate automatically)

`SECRET_KEY`, `DATABASE_URL`, `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`,
`MPESA_SHORTCODE`, `MPESA_TILL_NUMBER`, `MPESA_PASSKEY`, `MPESA_CALLBACK_URL`,
`CLOUDINARY_URL` **or** `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
`CLOUDINARY_API_SECRET`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_API_KEY`, `TWILIO_API_SECRET`, `TWILIO_FROM_NUMBER`, `NIA_ADMIN_PHONE`,
`NIA_ADMIN_CALLER_PHONE`, `NIA_CRON_SECRET`, `OPENAI_API_KEY`,
`GOOGLE_MAPS_API_KEY`, `GOOGLE_MAPS_MAP_ID`, `EMAIL_HOST_PASSWORD`,
`DEFAULT_FROM_EMAIL`, `AFRICASTALKING_API_KEY`, `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `COOP_CONNECT_SIT_CLIENT_ID`,
`COOP_CONNECT_SIT_CLIENT_SECRET`, `COOP_CONNECT_SIT_USER_ID`, and any enabled
provider webhook signing secret such as `STRIPE_WEBHOOK_SECRET`.

## Deployment and investor handover checklist

1. Protect `main`, require review and successful release checks, and record the audited commit SHA in the release ticket.
2. Confirm no local `.env`, database dumps, provider logs, or Gradle caches are committed; use Render secret references for all credentials.
3. Run the commands in the release-check section of the handover ticket with production-equivalent non-secret configuration.
4. Confirm `/health/`, customer checkout (non-live/sandbox where appropriate), one staff route, seller route, delivery route, and provider webhook verification behavior after deployment.
5. Confirm Render’s deployed commit equals the reviewed SHA. Roll back by commit, not by editing production files.
6. Give investor handover recipients: secret-owner map, key rotation runbook, incident contacts, database backup/restore evidence, provider-account ownership, access-review date, webhook validation evidence, and this residual-risk register.
7. Complete CSP reporting/inventory before switching it from report-only to enforcement; test checkout, maps, Cloudinary images, Nia, and admin in staging first.

## Residual risks

This release is not a claim of complete security. Public third-party callbacks
remain dependent on provider-supported signature/source configuration; some
legacy GET cart links remain a low-impact cross-site state-change compatibility
risk until converted to CSRF-protected forms; inline scripts prevent immediate
strict CSP enforcement; and live Render/provider configuration and dependency
advisories require separate authenticated operational verification.
