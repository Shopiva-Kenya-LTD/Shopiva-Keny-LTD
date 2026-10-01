# Shopiva Kenya Security Handover

## Scope
This document records the security hardening work verified against the `main` branch and the remaining release-gate items. It is not a claim that Shopiva is impossible to breach.

## Verified baseline

- Production `DEBUG` defaults to false and production requires `SECRET_KEY`.
- Production hosts and CSRF trusted origins are explicitly configured.
- Secure session/CSRF cookies, HTTPS proxy handling, HSTS, MIME-sniffing protection, Referrer-Policy, clickjacking protection, and cross-origin policies are configured in `shopiva/settings.py`.
- Django Axes is enabled for authentication failure throttling/lockout.
- Production deployment configuration includes `check --deploy`, migrations, system preflight, and static collection in `render.yaml`.
- M-PESA callbacks validate CheckoutRequestID, receipt presence, amount, and phone before marking a payment paid.
- Stripe webhook processing requires a valid Stripe signature.
- Twilio Nia inbound/verification webhooks validate the Twilio signature.
- Nia cron execution requires a secret header and constant-time comparison.
- Admin/staff access is separated from customer/seller surfaces by the admin portal boundary middleware.
- Seller settlement creation uses an idempotent database operation.
- Delivery payout rules remain manual-admin payout after the delivery/receipt workflow; no automatic B2C payout is introduced by this hardening pass.
- Admin logout was hardened to require POST, so a cross-site GET cannot terminate an administrator session.
- Regression tests were added for the admin logout method boundary.

## Release blockers / follow-up

1. Run the full CI/release gate and `python manage.py check --deploy` against production settings.
2. Resolve the Render configuration drift: the live Render service has historically used a migrate-only build and ASGI start command while `render.yaml` defines a stricter WSGI build/start path. This must be reconciled deliberately before treating the deployment configuration as final.
3. Review all remaining `csrf_exempt` endpoints individually. Provider callbacks that cannot carry Django CSRF tokens must retain independent provider authentication/signature validation.
4. Replace short-lived Nia caller PIN plaintext storage with a one-way hash in a separate migration, then verify the complete inbound-call flow.
5. Review file-upload validation and Cloudinary/media handling against an allowlist of content types, size limits, and safe processing.
6. Review authorization for every admin, seller, delivery, payment, payout, and Nia action for object-level access control.
7. Add/maintain security regression tests for payment replay, callback tampering, IDOR/object ownership, privilege boundaries, and payout state transitions.
8. Rotate any credential that has ever been exposed outside the secret store. Do not rotate production credentials automatically during deployment.

## Deployment rule

Do not declare a security release complete merely because a deployment becomes `live`. The release gate must pass, the deployed commit must be identified, and security-sensitive behavior must be tested after deployment.

## Security objective

Use defense in depth: least privilege, server-side authorization, strict state transitions, provider verification, idempotency, secure cookies, HTTPS, safe headers, dependency updates, audit logging, and automated regression tests.
