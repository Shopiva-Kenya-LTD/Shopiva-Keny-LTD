# Shopiva Google Maps Production Setup

Shopiva uses Google Maps JavaScript API, Places API (New) / Places Library, and AdvancedMarkerElement for seller pickup, customer delivery addresses, checkout location capture and delivery tracking. The Admin Android app opens the protected Shopiva web admin and now has a dedicated **Live Delivery Map** button that opens `/admin/google-delivery-map/`.

## Google Cloud project

1. Open Google Cloud Console and create/select the Shopiva project.
2. Attach a billing account. Google requires billing for production Maps JavaScript usage.
3. Enable **Maps JavaScript API**.
4. Enable **Places API (New)**. Places Autocomplete uses the Places Library.
5. Create a **JavaScript map ID**. `DEMO_MAP_ID` is only a testing fallback; production should use the Shopiva map ID.
6. Create a browser API key. Do not commit it to GitHub.

## Browser key restrictions

Application restriction: **Websites (HTTP referrers)**.

Authorized website referrer for the current free Render hostname:

`https://shopiva-keny-ltd.onrender.com/*`

When the paid domain is purchased, add the new production hostname too, for example:

`https://shopivakenya.co.ke/*`
`https://www.shopivakenya.co.ke/*`

API restrictions for the browser key:

- Maps JavaScript API
- Places API
- Places API (New)

Do not use this browser key for server-to-server Google web services. Create a separate appropriately restricted server key whenever a Google web service is added to the Django backend.

## Android Admin app

The current Admin APK is a secure WebView shell. It does **not** embed a second native Google Maps API key: the map is rendered by the Shopiva web application using the browser-restricted JavaScript key. This keeps the architecture simpler and avoids duplicating map credentials.

Required Android basics already present:

- `INTERNET` permission
- HTTPS-only traffic (`usesCleartextTraffic=false`)
- JavaScript and DOM storage enabled for the admin WebView
- Admin authentication remains enforced by Django staff/superuser checks

A native Maps SDK for Android key is only required later if the Admin APK is changed to render maps with the native Google Maps SDK instead of the existing WebView map.

## Render environment

Set these variables on the `Shopiva-Keny-LTD` service:

`GOOGLE_MAPS_API_KEY=<browser key>`

`GOOGLE_MAPS_MAP_ID=<production JavaScript map ID>`

`PUBLIC_SITE_URL=https://shopivakenya.top`

Never paste credentials into repository source files. The browser key is intentionally visible at runtime; its protection comes from HTTP-referrer and API restrictions.

## Shopiva map surfaces

- Seller product listing requires a pickup/business address plus latitude and longitude.
- Customer saved delivery addresses require a map pin.
- Checkout captures delivery latitude/longitude.
- Delivery partners can share live GPS while the delivery workspace is active.
- Customer tracking endpoints expose delivery coordinates only for the signed-in customer's own orders.
- Admin delivery mapping consumes the protected rider-location endpoint.
- Admin Android now exposes the same Google delivery operations screen through its `Live Delivery Map` control.

## Search / discoverability requirements

For search engines, the site also needs:

- `robots.txt` allowing public pages and pointing to the sitemap
- `sitemap.xml` listing the public homepage, catalogue, categories, install page and active product URLs
- canonical URLs using `PUBLIC_SITE_URL`
- index/follow metadata on public HTML
- Product/OnlineStore structured data on public pages
- Google Search Console property verification
- sitemap submission in Google Search Console
- optional Bing Webmaster Tools verification + sitemap submission / IndexNow for faster change discovery

## Important

Google Maps cannot be made production-live by code alone without a valid Google Cloud project, billing, enabled APIs, a restricted browser key and a production map ID. Google currently documents API-key authentication and billing as required for production Maps JavaScript use, and AdvancedMarkerElement requires a map ID. Places Autocomplete requires the Places library and Places API (New). See the official Google documentation linked from the project documentation.
