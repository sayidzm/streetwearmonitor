#!/usr/bin/env python3
"""Streetwear Monitor: kimlik, envanter ve idempotent olay katmanı."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scanners import ShopifyScanner, SitemapScanner
from scanners.base import normalize, price_value
from scanners.sitemap import title_from_url

ROOT = Path(__file__).resolve().parent
CONFIG, STATE = ROOT / "magazalar.json", ROOT / "veri.json"
SETTINGS, FAVORITES = ROOT / "ayarlar.json", ROOT / "favoriler.json"
STATE_VERSION = 3
EVENT_HISTORY_LIMIT = 1000
DEFAULT_SETTINGS: dict[str, Any] = {
    "telegram": {
        "notify_new": True, "notify_price_drops": True, "notify_stock": True,
        "notify_price_increases": False, "only_favorites": False, "brands": [],
        "include_keywords": [], "exclude_keywords": [], "minimum_discount_percent": 0,
    },
    "scan": {"max_workers": 4, "max_products_per_store": 15000, "minimum_catalog_ratio": 0.25},
}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def atomic_json(path: Path, value: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".streetwear-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name} okunamadı: {error}") from error


def merge_defaults(value: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    if not isinstance(value, dict):
        return result
    for key, default in defaults.items():
        supplied = value.get(key)
        result[key] = merge_defaults(supplied, default) if isinstance(default, dict) else supplied if supplied is not None else default
    result.update({key: supplied for key, supplied in value.items() if key not in result})
    return result


def digest(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]


def stable_store_id(site: dict[str, Any]) -> str:
    return str(site.get("id") or f"store_{digest(normalize(str(site['url'])))}")


def ensure_store_ids(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    for site in config.get("sites", []) if isinstance(config.get("sites"), list) else []:
        if isinstance(site, dict) and site.get("url") and not site.get("id"):
            site["id"] = stable_store_id(site)
            changed = True
    return config, changed


def legacy_product(raw: Any, key: str) -> dict[str, Any] | None:
    item = dict(raw) if isinstance(raw, dict) else {}
    url = item.get("url") or key
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    url = normalize(url)
    product_id = str(item.get("id") or f"url:{url}")
    variants = item.get("variants") if isinstance(item.get("variants"), dict) else {}
    return {
        "id": product_id, "name": str(item.get("name") or title_from_url(url)), "url": url,
        "image": item.get("image"), "price": price_value(item.get("price")),
        "compare_at_price": price_value(item.get("compare_at_price")), "currency": item.get("currency"),
        "available": item.get("available"), "variants": variants,
    }


def migrate_state(raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    config = config or {"sites": []}
    by_name = {str(site.get("name")): stable_store_id(site) for site in config.get("sites", []) if isinstance(site, dict) and site.get("url")}
    stores: dict[str, dict[str, Any]] = {}
    for old_key, record in (raw.get("stores", {}) if isinstance(raw.get("stores"), dict) else {}).items():
        record = record if isinstance(record, dict) else {}
        store_id = old_key if str(old_key).startswith("store_") else by_name.get(str(old_key), f"legacy_{digest(old_key)}")
        products_raw = record.get("products") if isinstance(record.get("products"), dict) else {url: {"url": url} for url in record.get("seen", []) if isinstance(url, str)}
        products = {}
        for key, item in products_raw.items():
            converted = legacy_product(item, str(key))
            if converted:
                products[converted["id"]] = converted
        previous = stores.get(store_id, {})
        previous["products"] = {**previous.get("products", {}), **products}
        previous.update({
            "id": store_id, "name": record.get("name") or old_key, "url": record.get("url"),
            "count": int(record.get("count", len(previous["products"]))), "checked": record.get("checked", "-"),
            "last_error": record.get("last_error"),
            "baseline_pending": True if raw.get("version") != STATE_VERSION else bool(record.get("baseline_pending", False)),
        })
        stores[store_id] = previous
    pending = []
    for item in raw.get("pending", []) if isinstance(raw.get("pending"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            event = dict(item)
            event.setdefault("type", event.pop("kind", "NEW_PRODUCT").upper())
            event.setdefault("id", "legacy_" + digest(event["type"], event.get("brand", ""), event["url"]))
            pending.append(event)
    history = raw.get("event_history", []) if isinstance(raw.get("event_history"), list) else []
    history_ids = {item.get("id") for item in history if isinstance(item, dict)} | set(raw.get("sent", {}).keys() if isinstance(raw.get("sent"), dict) else [])
    return {"version": STATE_VERSION, "stores": stores, "pending": pending,
            "event_history": history[-EVENT_HISTORY_LIMIT:], "event_ids": sorted(item for item in history_ids if isinstance(item, str))[-EVENT_HISTORY_LIMIT:]}


def scan(site: dict[str, Any]) -> dict[str, dict[str, Any]]:
    method = site.get("method", "auto")
    if method not in {"auto", "shopify", "sitemap"}:
        raise ValueError("Bilinmeyen tarama yöntemi")
    if method in {"auto", "shopify"}:
        try:
            found = ShopifyScanner().scan(site)
            if found:
                return found
            raise ValueError("Shopify ürün listesi boş")
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
            if method == "shopify":
                raise
    return SitemapScanner().scan(site)


def product_match(current: dict[str, Any], old_products: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if current["id"] in old_products:
        return old_products[current["id"]]
    url = normalize(current["url"])
    return next((old for old in old_products.values() if isinstance(old, dict) and old.get("url") and normalize(str(old["url"])) == url), None)


def event_id(event_type: str, store_id: str, product_id: str, variant_id: str | None = None, before: Any = None, after: Any = None) -> str:
    return "evt_" + digest(event_type, store_id, product_id, variant_id or "", before if before is not None else "", after if after is not None else "")


def event(event_type: str, store: dict[str, Any], product: dict[str, Any], *, variant: dict[str, Any] | None = None, before: Any = None, after: Any = None) -> dict[str, Any]:
    old_price, new_price = (before, after) if event_type in {"PRICE_DROP", "PRICE_INCREASE"} else (None, None)
    data = {
        "type": event_type, "store_id": store["id"], "store_name": store["name"], "product_id": product["id"],
        "product_name": product["name"], "url": product["url"], "image": product.get("image"), "currency": product.get("currency"),
        "variant_id": variant.get("id") if variant else None, "variant_title": variant.get("title") if variant else None,
        "old_price": old_price, "new_price": new_price, "before": before, "after": after,
    }
    if old_price not in (None, 0) and new_price is not None:
        data["change_percent"] = round((new_price - old_price) / old_price * 100, 2)
    data["id"] = event_id(event_type, data["store_id"], data["product_id"], data["variant_id"], before, after)
    return data


def compare_products(store: dict[str, Any], current: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in current.values():
        old = product_match(item, previous)
        if old is None:
            events.append(event("NEW_PRODUCT", store, item))
            continue
        if old.get("available") is False and item.get("available") is True:
            events.append(event("RESTOCK", store, item, before=False, after=True))
        if old.get("available") is True and item.get("available") is False:
            events.append(event("SOLD_OUT", store, item, before=True, after=False))
        old_price, new_price = price_value(old.get("price")), price_value(item.get("price"))
        if old_price is not None and new_price is not None and old_price != new_price:
            events.append(event("PRICE_DROP" if new_price < old_price else "PRICE_INCREASE", store, item, before=old_price, after=new_price))
        old_variants = old.get("variants") if isinstance(old.get("variants"), dict) else {}
        for variant_id, variant in (item.get("variants") or {}).items():
            prior = old_variants.get(variant_id)
            if not isinstance(prior, dict):
                continue
            if prior.get("available") is False and variant.get("available") is True:
                events.append(event("VARIANT_RESTOCK", store, item, variant=variant, before=False, after=True))
            if prior.get("available") is True and variant.get("available") is False:
                events.append(event("VARIANT_SOLD_OUT", store, item, variant=variant, before=True, after=False))
    return events


def favorite_urls() -> set[str]:
    raw = load_json(FAVORITES, [])
    values = raw.get("favorites", []) if isinstance(raw, dict) else raw
    return {normalize(item["url"] if isinstance(item, dict) else item) for item in values if isinstance(item, str) or isinstance(item, dict) and isinstance(item.get("url"), str)}


def allowed(item: dict[str, Any], settings: dict[str, Any], favorites: set[str]) -> bool:
    rules, event_type = settings["telegram"], item.get("type", "NEW_PRODUCT")
    if event_type == "NEW_PRODUCT" and not rules["notify_new"] or event_type == "PRICE_DROP" and not rules["notify_price_drops"]:
        return False
    if event_type in {"RESTOCK", "SOLD_OUT", "VARIANT_RESTOCK", "VARIANT_SOLD_OUT"} and not rules.get("notify_stock", True):
        return False
    if event_type == "PRICE_INCREASE" and not rules.get("notify_price_increases", False):
        return False
    if rules["only_favorites"] and normalize(item["url"]) not in favorites:
        return False
    brands = {str(value).casefold() for value in rules.get("brands", [])}
    if brands and item.get("store_name", item.get("brand", "")).casefold() not in brands:
        return False
    text = f"{item.get('store_name', item.get('brand', ''))} {item.get('product_name', item.get('name', ''))}".casefold()
    include = [str(value).casefold() for value in rules.get("include_keywords", []) if str(value).strip()]
    exclude = [str(value).casefold() for value in rules.get("exclude_keywords", []) if str(value).strip()]
    if include and not any(value in text for value in include) or any(value in text for value in exclude):
        return False
    return not (event_type == "PRICE_DROP" and item.get("change_percent", 0) > -float(rules.get("minimum_discount_percent", 0)))


def money(value: Any, currency: str | None) -> str:
    return f"{value:,.2f} {currency or ''}".strip() if isinstance(value, (int, float)) else "—"


def telegram(items: list[dict[str, Any]]) -> bool:
    token, chat_id = os.getenv("STREETWEAR_BOT_TOKEN"), os.getenv("STREETWEAR_CHAT_ID")
    if not token or not chat_id:
        return False
    for offset in range(0, len(items), 8):
        lines = ["Streetwear Takip"]
        for item in items[offset:offset + 8]:
            price = ""
            if item.get("type") in {"PRICE_DROP", "PRICE_INCREASE"}:
                price = f" — {money(item.get('old_price'), item.get('currency'))} → {money(item.get('new_price'), item.get('currency'))}"
            lines.append(f"• {html.escape(item.get('type', 'EVENT'))} | {html.escape(item.get('store_name', item.get('brand', '')))}: <a href=\"{html.escape(item['url'], quote=True)}\">{html.escape(item.get('product_name', item.get('name', ''))[:110])}</a>{price}")
        payload = json.dumps({"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
        result = json.loads(urlopen(Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers={"Content-Type": "application/json"}), timeout=25).read())
        if not result.get("ok"):
            raise ValueError("Telegram bildirimi reddetti")
    return True


def queue_events(state: dict[str, Any], events: list[dict[str, Any]]) -> None:
    known = set(state.get("event_ids", [])) | {item.get("id") for item in state.get("pending", []) if isinstance(item, dict)}
    fresh = [item for item in events if item["id"] not in known]
    state["pending"].extend(fresh)
    history = state.get("event_history", []) + fresh
    state["event_history"] = history[-EVENT_HISTORY_LIMIT:]
    state["event_ids"] = [item["id"] for item in state["event_history"]][-EVENT_HISTORY_LIMIT:]


def check(args: argparse.Namespace) -> int:
    config = load_json(CONFIG, {})
    if not isinstance(config.get("sites"), list):
        raise ValueError("magazalar.json içindeki sites bir liste olmalı")
    config, config_changed = ensure_store_ids(config)
    if config_changed:
        atomic_json(CONFIG, config)
    settings, state = merge_defaults(load_json(SETTINGS, {}), DEFAULT_SETTINGS), migrate_state(load_json(STATE, {}), config)
    sites = [site for site in config["sites"] if isinstance(site, dict) and site.get("enabled", True)]
    if args.status:
        print(f"Etkin site: {len(sites)} | Bekleyen bildirim: {len(state['pending'])}")
        for site in sites:
            record = state["stores"].get(stable_store_id(site), {})
            print(f"{site.get('name', '?')}: {record.get('count', 'henüz taranmadı')} ürün, {record.get('checked', '-')}")
        return 0
    if args.test_bildirim:
        if not telegram([{"type": "TEST", "store_name": "Test", "product_name": "Bildirim bağlantısı çalışıyor", "url": "https://example.com"}]):
            print("Önce STREETWEAR_BOT_TOKEN ve STREETWEAR_CHAT_ID ayarla")
            return 2
        print("Test bildirimi gönderildi")
        return 0
    max_products, ratio = int(settings["scan"].get("max_products_per_store", 15000)), float(settings["scan"].get("minimum_catalog_ratio", .25))
    failures, discovered = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, int(settings["scan"].get("max_workers", 4))))) as pool:
        jobs = {pool.submit(scan, site): site for site in sites}
        for future in concurrent.futures.as_completed(jobs):
            site, store_id = jobs[future], stable_store_id(jobs[future])
            name = site.get("name", "Adsız mağaza")
            try:
                current, old_record = future.result(), state["stores"].get(store_id)
                if len(current) > int(site.get("max_products", max_products)):
                    raise ValueError(f"Katalog güvenlik sınırını aştı ({len(current)} ürün)")
                previous = old_record.get("products", {}) if old_record else {}
                if previous and len(current) < max(1, int(old_record.get("count", len(previous)) * ratio)):
                    raise ValueError("Ürün sayısı şüpheli biçimde azaldı; önceki kayıt korundu")
                store = {"id": store_id, "name": name, "url": site["url"]}
                is_baseline = not old_record or bool(old_record.get("baseline_pending"))
                events = [] if is_baseline else compare_products(store, current, previous)
                discovered.extend(events)
                state["stores"][store_id] = {**store, "products": current, "count": len(current), "checked": now(), "last_error": None, "baseline_pending": False}
                print(f"{name}: {len(current)} ürün, {len(events)} yeni olay" if old_record else f"{name}: ilk güvenilir tarama, {len(current)} ürün kaydedildi")
            except Exception as error:
                failures.append(name)
                record = state["stores"].setdefault(store_id, {"id": store_id, "name": name, "url": site["url"], "products": {}, "count": 0, "checked": "-", "baseline_pending": True})
                record["last_error"] = f"{now()}: {error}"
                print(f"{name}: HATA: {error}", file=sys.stderr)
    queue_events(state, discovered)
    to_send = [item for item in state["pending"] if isinstance(item, dict) and allowed(item, settings, favorite_urls())]
    if args.dry_run:
        print(f"Kuru çalışma: {len(to_send)} Telegram bildirimi gönderilmeden listelendi.")
    elif to_send:
        try:
            if telegram(to_send):
                sent_ids = {item["id"] for item in to_send}
                state["pending"] = [item for item in state["pending"] if item.get("id") not in sent_ids]
                print(f"{len(sent_ids)} olay Telegram ile bildirildi")
            else:
                print(f"{len(to_send)} olay bulundu. Telegram ayarlanmadı; bildirim beklemede.")
        except Exception as error:
            print(f"Bildirim hatası: {error}; olaylar kuyrukta kaldı", file=sys.stderr)
            atomic_json(STATE, state)
            return 2
    atomic_json(STATE, state)
    if failures:
        print(f"Başarısız mağaza sayısı: {len(failures)}. Kayıtlar silinmedi.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Streetwear ürün, stok ve fiyat olaylarını kontrol et")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--test-bildirim", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return check(parser.parse_args())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"HATA: {error}", file=sys.stderr)
        raise SystemExit(2)