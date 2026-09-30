"""Shopiva's public MCP server for ChatGPT.

This server exposes only live, public Shopiva data. Customer, seller, delivery-agent,
and admin data must stay behind Shopiva authentication and is intentionally not
exposed by these public tools.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shopiva.settings")

import django

django.setup()

from django.db.models import Q

from home.models import (
    DeliveryPricingProfile,
    DeliveryTariff,
    Product,
    ShopivaBranch,
    ShopivaOutlet,
)
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


SITE_URL = os.getenv("PUBLIC_SITE_URL", "https://shopivakenya.top").rstrip("/")

mcp = MCPServer(
    "nia-shopiva-kenya",
    title="Nia for Shopiva Kenya",
    description="Live Shopiva Kenya shopping, product, location and delivery information.",
    instructions=(
        "You are connected to Shopiva Kenya's live public data. "
        "Use the Shopiva tools when the user asks about products, prices, stock, "
        "categories, Shopiva locations, or published delivery tariffs. "
        "Never invent Shopiva data. These tools are public and do not expose customer, "
        "seller, delivery-agent, payment, or administrator records."
    ),
    website_url=SITE_URL,
    version="1.0.0",
)


@mcp.tool(
    title="Search Shopiva products",
    description=(
        "Search the live Shopiva catalogue by product name, category, description, "
        "or price ceiling. Returns active products only."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def search_shopiva_products(
    query: str = "",
    category: str = "",
    max_price: float | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Search Shopiva's live public product catalogue."""
    limit = max(1, min(int(limit), 20))
    qs = Product.objects.filter(is_active=True).order_by("-is_featured", "name")

    query = (query or "").strip()
    category = (category or "").strip()

    if query:
        qs = qs.filter(
            Q(name__icontains=query)
            | Q(category__icontains=query)
            | Q(description__icontains=query)
            | Q(brand__icontains=query)
        )
    if category:
        qs = qs.filter(category__icontains=category)
    if max_price is not None:
        qs = qs.filter(price__lte=max_price)

    products = []
    for product in qs[:limit]:
        products.append(_product_payload(product))

    return {
        "site": SITE_URL,
        "count": len(products),
        "products": products,
    }


@mcp.tool(
    title="Get a Shopiva product",
    description="Get current public details for one active Shopiva product by numeric product ID.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def get_shopiva_product(product_id: int) -> dict[str, Any]:
    """Return one active public Shopiva product."""
    product = Product.objects.filter(id=product_id, is_active=True).first()
    if not product:
        return {"found": False, "error": "Product not found or no longer available."}
    return {"found": True, "product": _product_payload(product)}


@mcp.tool(
    title="List Shopiva categories",
    description="List categories currently represented by active Shopiva products.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def list_shopiva_categories() -> dict[str, Any]:
    """Return live active product categories."""
    categories = sorted(
        {
            (value or "General").strip()
            for value in Product.objects.filter(is_active=True)
            .values_list("category", flat=True)
            if (value or "").strip()
        },
        key=str.casefold,
    )
    return {"site": SITE_URL, "categories": categories}


@mcp.tool(
    title="Find Shopiva locations",
    description="List active Shopiva branches and outlets that are currently published.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def find_shopiva_locations(
    town: str = "",
    county: str = "",
) -> dict[str, Any]:
    """Return active Shopiva branches and outlets matching optional location filters."""
    town = (town or "").strip()
    county = (county or "").strip()

    branches = ShopivaBranch.objects.filter(is_active=True)
    outlets = ShopivaOutlet.objects.filter(is_active=True)

    if town:
        branches = branches.filter(town__icontains=town)
        outlets = outlets.filter(town__icontains=town)
    if county:
        branches = branches.filter(county__icontains=county)
        outlets = outlets.filter(county__icontains=county)

    return {
        "site": SITE_URL,
        "branches": [
            {
                "id": branch.id,
                "name": branch.name,
                "code": branch.code,
                "town": branch.town,
                "county": branch.county,
                "address": branch.address,
                "phone": branch.phone,
                "services": branch.services,
                "headquarters": branch.is_headquarters,
                "url": f"{SITE_URL}/",
            }
            for branch in branches[:50]
        ],
        "outlets": [
            {
                "id": outlet.id,
                "name": outlet.name,
                "code": outlet.code,
                "town": outlet.town,
                "county": outlet.county,
                "address": outlet.address,
                "phone": outlet.phone,
                "pickup_available": outlet.pickup_available,
                "url": f"{SITE_URL}/",
            }
            for outlet in outlets[:50]
        ],
    }


@mcp.tool(
    title="Check Shopiva delivery tariffs",
    description=(
        "Return currently active, published destination delivery tariffs. "
        "If a destination is absent, do not guess a price."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def check_shopiva_delivery_tariffs(
    destination: str = "",
    county: str = "",
) -> dict[str, Any]:
    """Return live configured destination tariffs without inventing unsupported prices."""
    qs = DeliveryTariff.objects.filter(is_active=True).order_by("county", "destination")
    destination = (destination or "").strip()
    county = (county or "").strip()

    if destination:
        qs = qs.filter(destination__icontains=destination)
    if county:
        qs = qs.filter(county__icontains=county)

    rows = [
        {
            "county": tariff.county,
            "destination": tariff.destination,
            "standard_fee_ksh": str(tariff.standard_fee),
            "pickup_fee_ksh": str(tariff.pickup_fee) if tariff.pickup_fee is not None else None,
            "express_fee_ksh": str(tariff.express_fee) if tariff.express_fee is not None else None,
            "pickup_available": tariff.pickup_available,
            "express_available": tariff.express_available,
            "fallback_for_county": tariff.is_fallback,
        }
        for tariff in qs[:100]
    ]
    return {
        "site": SITE_URL,
        "count": len(rows),
        "tariffs": rows,
        "pricing_rule": "No tariff is inferred when Shopiva has no configured destination price.",
    }


@mcp.tool(
    title="Shopiva delivery pricing status",
    description=(
        "Show the active delivery pricing profiles published by Shopiva. "
        "Useful for explaining whether distance pricing is active."
    ),
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def shopiva_delivery_pricing_status() -> dict[str, Any]:
    """Return active public delivery pricing configuration."""
    rows = [
        {
            "name": profile.name,
            "delivery_mode": profile.get_delivery_mode_display(),
            "pricing_mode": profile.get_pricing_mode_display(),
            "base_fee_ksh": str(profile.base_fee),
            "per_km_fee_ksh": str(profile.per_km_fee),
            "per_seller_fee_ksh": str(profile.per_seller_fee),
            "rural_surcharge_ksh": str(profile.rural_surcharge),
            "minimum_fee_ksh": str(profile.minimum_fee),
            "maximum_fee_ksh": str(profile.maximum_fee) if profile.maximum_fee is not None else None,
            "notes": profile.notes,
        }
        for profile in DeliveryPricingProfile.objects.filter(is_active=True)
    ]
    return {
        "site": SITE_URL,
        "active_profiles": rows,
        "configured": bool(rows),
    }


@mcp.tool(
    title="Open Shopiva",
    description="Return the public Shopiva Kenya storefront URL.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def open_shopiva() -> dict[str, str]:
    """Return the canonical Shopiva storefront."""
    return {
        "name": "Shopiva Kenya",
        "url": SITE_URL,
        "message": "Open Shopiva Kenya to browse products and use the authenticated customer account.",
    }


def _product_payload(product: Product) -> dict[str, Any]:
    discounted = product.discounted_price
    return {
        "id": product.id,
        "name": product.name,
        "brand": product.brand,
        "category": product.category,
        "description": (product.description or "")[:600],
        "price_ksh": str(product.price),
        "current_price_ksh": str(discounted),
        "discount_percent": product.discount_percent,
        "stock_quantity": product.stock_quantity,
        "in_stock": product.stock_quantity > 0,
        "featured": product.is_featured,
        "url": f"{SITE_URL}/product/{product.id}/",
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
    )
