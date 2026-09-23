"""Shopify products.json adapterı."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote, urljoin

from .base import Scanner, normalize, price_value


class ShopifyScanner(Scanner):
    def scan(self, site: dict[str, Any]) -> dict[str, dict[str, Any]]:
        base = site["url"].rstrip("/")
        currency = str(site.get("currency") or "UNKNOWN").upper()
        found: dict[str, dict[str, Any]] = {}
        for page in range(1, 31):
            payload = self.json(urljoin(base + "/", f"products.json?limit=250&page={page}"))
            products = payload.get("products") if isinstance(payload, dict) else None
            if not isinstance(products, list):
                raise ValueError("Shopify ürün yanıtı geçerli değil")
            for raw in products:
                if not isinstance(raw, dict) or raw.get("id") is None or not raw.get("handle"):
                    continue
                product_id = f"shopify:{raw['id']}"
                url = normalize(urljoin(base + "/", "products/" + quote(str(raw["handle"]), safe="-")))
                variants: dict[str, dict[str, Any]] = {}
                for variant in raw.get("variants", []) if isinstance(raw.get("variants"), list) else []:
                    if not isinstance(variant, dict) or variant.get("id") is None:
                        continue
                    variant_id = f"shopify:{variant['id']}"
                    variants[variant_id] = {
                        "id": variant_id,
                        "title": str(variant.get("title") or "Varsayılan başlık"),
                        "price": price_value(variant.get("price")),
                        "compare_at_price": price_value(variant.get("compare_at_price")),
                        "available": bool(variant.get("available", False)),
                    }
                prices = [item["price"] for item in variants.values() if item["price"] is not None]
                compares = [item["compare_at_price"] for item in variants.values() if item["compare_at_price"] is not None]
                images = raw.get("images") if isinstance(raw.get("images"), list) else []
                image = raw.get("image") if isinstance(raw.get("image"), dict) else None
                image_url = (image or (images[0] if images and isinstance(images[0], dict) else {})).get("src")
                found[product_id] = {
                    "id": product_id,
                    "name": str(raw.get("title") or "İsimsiz ürün").strip()[:300],
                    "url": url,
                    "image": str(image_url) if image_url else None,
                    "price": min(prices) if prices else None,
                    "compare_at_price": min(compares) if compares else None,
                    "currency": currency,
                    "available": any(item["available"] for item in variants.values()),
                    "variants": variants,
                }
            if len(products) < 250:
                return found
        raise ValueError("Shopify sayfa sınırı aşıldı; eksik katalog kaydedilmedi")
