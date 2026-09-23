#!/usr/bin/env python3
"""Streetwear Monitor: V4 product lifecycle and notification delivery."""
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
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scanners import ShopifyScanner, SitemapScanner
from scanners.base import normalize, price_value
from scanners.sitemap import title_from_url

ROOT = Path(__file__).resolve().parent
CONFIG, STATE = ROOT / "magazalar.json", ROOT / "veri.json"
SETTINGS, FAVORITES = ROOT / "ayarlar.json", ROOT / "favoriler.json"
SCHEMA_VERSION, EVENT_HISTORY_LIMIT, KNOWN_PRODUCTS_LIMIT = 4, 1000, 200000
DEFAULT_SETTINGS: dict[str, Any] = {
    "telegram": {"notify_new": True, "notify_price_drops": True, "notify_stock": True,
                 "notify_price_increases": False, "only_favorites": False, "brands": [],
                 "include_keywords": [], "exclude_keywords": [], "minimum_discount_percent": 0},
    "scan": {"max_workers": 4, "max_products_per_store": 15000, "minimum_catalog_ratio": 0.25,
             "known_products_limit": KNOWN_PRODUCTS_LIMIT},
}


def now() -> str: return time.strftime("%Y-%m-%d %H:%M:%S")


def atomic_json(path: Path, value: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".streetwear-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output: json.dump(value, output, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists(): return fallback
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error: raise ValueError(f"{path.name} okunamadı: {error}") from error


def merge_defaults(value: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    if not isinstance(value, dict): return result
    for key, default in defaults.items():
        supplied = value.get(key)
        result[key] = merge_defaults(supplied, default) if isinstance(default, dict) else supplied if supplied is not None else default
    result.update({key: supplied for key, supplied in value.items() if key not in result})
    return result


def digest(*parts: Any) -> str: return hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()[:24]


def stable_store_id(site: dict[str, Any]) -> str: return str(site.get("id") or f"store_{digest(normalize(str(site['url'])))}")


def ensure_store_ids(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    for site in config.get("sites", []) if isinstance(config.get("sites"), list) else []:
        if isinstance(site, dict) and site.get("url") and not site.get("id"):
            site["id"], changed = stable_store_id(site), True
    return config, changed


def legacy_product(raw: Any, key: str) -> dict[str, Any] | None:
    item, url = (dict(raw) if isinstance(raw, dict) else {}), (dict(raw).get("url") if isinstance(raw, dict) else None) or key
    if not isinstance(url, str) or not url.startswith(("http://", "https://")): return None
    url, product_id = normalize(url), str(item.get("id") or f"url:{normalize(url)}")
    return {"id": product_id, "name": str(item.get("name") or title_from_url(url)), "url": url,
            "image": item.get("image"), "price": price_value(item.get("price")),
            "compare_at_price": price_value(item.get("compare_at_price")), "currency": item.get("currency"),
            "available": item.get("available"), "variants": item.get("variants") if isinstance(item.get("variants"), dict) else {},
            "last_seen": item.get("last_seen")}


def _products(raw: Any) -> dict[str, dict[str, Any]]:
    result = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        item = legacy_product(value, str(key))
        if item: result[item["id"]] = item
    return result


def migrate_state(raw: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    raw, config = (raw if isinstance(raw, dict) else {}), (config or {"sites": []})
    version = raw.get("schema_version", raw.get("version", 0))
    by_name = {str(site.get("name")): stable_store_id(site) for site in config.get("sites", []) if isinstance(site, dict) and site.get("url")}
    stores: dict[str, dict[str, Any]] = {}
    for old_key, record in (raw.get("stores", {}) if isinstance(raw.get("stores"), dict) else {}).items():
        record = record if isinstance(record, dict) else {}
        store_id = old_key if str(old_key).startswith("store_") else by_name.get(str(old_key), f"legacy_{digest(old_key)}")
        current_raw = record.get("current_products") if isinstance(record.get("current_products"), dict) else record.get("products")
        if not isinstance(current_raw, dict): current_raw = {url: {"url": url} for url in record.get("seen", []) if isinstance(url, str)}
        current, known = _products(current_raw), _products(record.get("known_products"))
        known = {**current, **known}  # known copies retain historical detail where available
        existing = stores.get(store_id, {})
        existing.update({"id": store_id, "name": record.get("name") or old_key, "url": record.get("url"),
                         "current_products": {**existing.get("current_products", {}), **current},
                         "known_products": {**existing.get("known_products", {}), **known},
                         "count": int(record.get("count", len(current))), "checked": record.get("checked", "-"),
                         "last_error": record.get("last_error"), "transition_generations": record.get("transition_generations", {}) if isinstance(record.get("transition_generations"), dict) else {},
                         "baseline_pending": True if version != SCHEMA_VERSION else bool(record.get("baseline_pending", False))})
        stores[store_id] = existing
    history = [dict(item) for item in raw.get("event_history", []) if isinstance(item, dict)][-EVENT_HISTORY_LIMIT:]
    old_pending = [dict(item) for item in raw.get("pending", []) if isinstance(item, dict)]
    legacy_pending = [dict(item) for item in raw.get("legacy_pending", []) if isinstance(item, dict)] + old_pending
    for item in legacy_pending:
        item.setdefault("id", "legacy_" + digest(item.get("type", item.get("kind", "EVENT")), item.get("url", "")))
        item.setdefault("delivery_status", "legacy_not_queued")
    queue = [dict(item) for item in raw.get("delivery_queue", []) if isinstance(item, dict)] if version == SCHEMA_VERSION else []
    return {"schema_version": SCHEMA_VERSION, "version": SCHEMA_VERSION, "stores": stores,
            "event_history": history, "delivery_queue": queue, "legacy_pending": legacy_pending,
            "delivered_event_ids": list(raw.get("delivered_event_ids", []))[-EVENT_HISTORY_LIMIT:]}


def scan(site: dict[str, Any]) -> dict[str, dict[str, Any]]:
    method = site.get("method", "auto")
    if method not in {"auto", "shopify", "sitemap"}: raise ValueError("Bilinmeyen tarama yöntemi")
    if method in {"auto", "shopify"}:
        try:
            found = ShopifyScanner().scan(site)
            if found: return found
            raise ValueError("Shopify ürün listesi boş")
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
            if method == "shopify": raise
    return SitemapScanner().scan(site)


def product_match(current: dict[str, Any], products: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if current["id"] in products: return products[current["id"]]
    url = normalize(current["url"])
    return next((old for old in products.values() if isinstance(old, dict) and old.get("url") and normalize(str(old["url"])) == url), None)


def occurrence_key(event_type: str, product_id: str, variant_id: str | None) -> str: return f"{event_type}|{product_id}|{variant_id or ''}"


def event_id(event_type: str, store_id: str, product_id: str, variant_id: str | None = None, before: Any = None, after: Any = None, occurrence: int = 1) -> str:
    return "evt_" + digest(event_type, store_id, product_id, variant_id or "", before if before is not None else "", after if after is not None else "", occurrence)


def make_event(record: dict[str, Any], event_type: str, store: dict[str, Any], product: dict[str, Any], *, variant: dict[str, Any] | None = None, before: Any = None, after: Any = None) -> dict[str, Any]:
    variant_id = variant.get("id") if variant else None
    key = occurrence_key(event_type, product["id"], variant_id)
    generation = int(record.setdefault("transition_generations", {}).get(key, 0)) + 1
    record["transition_generations"][key] = generation
    data = {"type": event_type, "store_id": store["id"], "store_name": store["name"], "product_id": product["id"],
            "product_name": product["name"], "url": product["url"], "image": product.get("image"), "currency": product.get("currency"),
            "variant_id": variant_id, "variant_title": variant.get("title") if variant else None, "before": before, "after": after,
            "occurrence": {"key": key, "generation": generation}, "created_at": now(), "delivery_status": "not_evaluated"}
    if event_type in {"PRICE_DROP", "PRICE_INCREASE"}:
        data["old_price"], data["new_price"] = before, after
        if before not in (None, 0) and after is not None: data["change_percent"] = round((after - before) / before * 100, 2)
    data["id"] = event_id(event_type, store["id"], product["id"], variant_id, before, after, generation)
    return data


def event(event_type: str, store: dict[str, Any], product: dict[str, Any], *, variant: dict[str, Any] | None = None, before: Any = None, after: Any = None, occurrence: int = 1) -> dict[str, Any]:
    record = {"transition_generations": {}}
    for _ in range(max(1, occurrence)): result = make_event(record, event_type, store, product, variant=variant, before=before, after=after)
    return result


def compare_store(record: dict[str, Any], store: dict[str, Any], current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    previous, known, events = record.get("current_products", {}), record.get("known_products", {}), []
    for item in current.values():
        old_current, old_known = product_match(item, previous), product_match(item, known)
        if old_known is None:
            events.append(make_event(record, "NEW_PRODUCT", store, item))
        elif old_current is None:
            events.append(make_event(record, "PRODUCT_RETURNED", store, item, before="missing", after="current"))
        else:
            if old_current.get("available") is False and item.get("available") is True: events.append(make_event(record, "RESTOCK", store, item, before=False, after=True))
            if old_current.get("available") is True and item.get("available") is False: events.append(make_event(record, "SOLD_OUT", store, item, before=True, after=False))
            old_price, new_price = price_value(old_current.get("price")), price_value(item.get("price"))
            if old_price is not None and new_price is not None and old_price != new_price:
                events.append(make_event(record, "PRICE_DROP" if new_price < old_price else "PRICE_INCREASE", store, item, before=old_price, after=new_price))
            for variant_id, variant in (item.get("variants") or {}).items():
                prior = (old_current.get("variants") or {}).get(variant_id)
                if not isinstance(prior, dict): continue
                if prior.get("available") is False and variant.get("available") is True: events.append(make_event(record, "VARIANT_RESTOCK", store, item, variant=variant, before=False, after=True))
                if prior.get("available") is True and variant.get("available") is False: events.append(make_event(record, "VARIANT_SOLD_OUT", store, item, variant=variant, before=True, after=False))
        item["last_seen"] = now()
    return events


def compare_products(store: dict[str, Any], current: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return compare_store({"current_products": previous, "known_products": previous, "transition_generations": {}}, store, current)


def merge_known(record: dict[str, Any], current: dict[str, dict[str, Any]], limit: int) -> None:
    known = record.setdefault("known_products", {})
    known.update(current)
    if len(known) <= limit: return
    current_ids = set(current)
    removable = sorted((item for product_id, item in known.items() if product_id not in current_ids for item in [(str(item.get("last_seen") or ""), product_id)]))
    for _, product_id in removable[:max(0, len(known) - limit)]: known.pop(product_id, None)


def favorite_urls() -> set[str]:
    raw = load_json(FAVORITES, [])
    values = raw.get("favorites", []) if isinstance(raw, dict) else raw
    return {normalize(item["url"] if isinstance(item, dict) else item) for item in values if isinstance(item, str) or isinstance(item, dict) and isinstance(item.get("url"), str)}


def allowed(item: dict[str, Any], settings: dict[str, Any], favorites: set[str]) -> bool:
    rules, kind = settings["telegram"], item.get("type", "NEW_PRODUCT")
    if kind == "NEW_PRODUCT" and not rules["notify_new"] or kind == "PRICE_DROP" and not rules["notify_price_drops"]: return False
    if kind in {"RESTOCK", "SOLD_OUT", "VARIANT_RESTOCK", "VARIANT_SOLD_OUT", "PRODUCT_RETURNED"} and not rules.get("notify_stock", True): return False
    if kind == "PRICE_INCREASE" and not rules.get("notify_price_increases", False): return False
    if rules["only_favorites"] and normalize(item["url"]) not in favorites: return False
    brands = {str(value).casefold() for value in rules.get("brands", [])}
    if brands and item.get("store_name", item.get("brand", "")).casefold() not in brands: return False
    text = f"{item.get('store_name', item.get('brand', ''))} {item.get('product_name', item.get('name', ''))}".casefold()
    include = [str(value).casefold() for value in rules.get("include_keywords", []) if str(value).strip()]
    exclude = [str(value).casefold() for value in rules.get("exclude_keywords", []) if str(value).strip()]
    if include and not any(value in text for value in include) or any(value in text for value in exclude): return False
    return not (kind == "PRICE_DROP" and item.get("change_percent", 0) > -float(rules.get("minimum_discount_percent", 0)))


def record_events(state: dict[str, Any], events: list[dict[str, Any]], settings: dict[str, Any], favorites: set[str]) -> None:
    queued = {item.get("id") for item in state.get("delivery_queue", []) if isinstance(item, dict)}
    history = state.setdefault("event_history", [])
    known_history = {item.get("id") for item in history if isinstance(item, dict)}
    for item in events:
        if item["id"] in known_history: continue
        if allowed(item, settings, favorites):
            item["delivery_status"] = "queued"
            if item["id"] not in queued: state.setdefault("delivery_queue", []).append(dict(item))
        else: item["delivery_status"] = "skipped_policy"
        history.append(item)
        known_history.add(item["id"])
    state["event_history"] = history[-EVENT_HISTORY_LIMIT:]


def queue_events(state: dict[str, Any], events: list[dict[str, Any]], settings: dict[str, Any] | None = None, favorites: set[str] | None = None) -> None:
    record_events(state, events, settings or merge_defaults({}, DEFAULT_SETTINGS), favorites or set())


def money(value: Any, currency: str | None) -> str: return f"{value:,.2f} {currency or 'UNKNOWN'}" if isinstance(value, (int, float)) else "—"


def telegram_batch(items: list[dict[str, Any]]) -> bool:
    token, chat_id = os.getenv("STREETWEAR_BOT_TOKEN"), os.getenv("STREETWEAR_CHAT_ID")
    if not token or not chat_id: return False
    lines = ["Streetwear Takip"]
    for item in items:
        price = f" — {money(item.get('old_price'), item.get('currency'))} → {money(item.get('new_price'), item.get('currency'))}" if item.get("type") in {"PRICE_DROP", "PRICE_INCREASE"} else ""
        lines.append(f"• {html.escape(item.get('type', 'EVENT'))} | {html.escape(item.get('store_name', item.get('brand', '')))}: <a href=\"{html.escape(item['url'], quote=True)}\">{html.escape(item.get('product_name', item.get('name', ''))[:110])}</a>{price}")
    payload = json.dumps({"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    result = json.loads(urlopen(Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers={"Content-Type": "application/json"}), timeout=25).read())
    if not result.get("ok"): raise ValueError("Telegram bildirimi reddetti")
    return True


def mark_delivered(state: dict[str, Any], ids: set[str]) -> None:
    state["delivery_queue"] = [item for item in state.get("delivery_queue", []) if item.get("id") not in ids]
    state["delivered_event_ids"] = (state.get("delivered_event_ids", []) + sorted(ids))[-EVENT_HISTORY_LIMIT:]
    for item in state.get("event_history", []):
        if item.get("id") in ids: item["delivery_status"] = "delivered"


def deliver_batches(state: dict[str, Any], sender: Callable[[list[dict[str, Any]]], bool], persist: Callable[[], None], batch_size: int = 8) -> int:
    state["delivery_queue"].sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))))
    delivered = 0
    while state.get("delivery_queue"):
        batch = state["delivery_queue"][:batch_size]
        if not sender(batch): break
        mark_delivered(state, {item["id"] for item in batch})
        persist()  # A crash after this point cannot resend this successful batch.
        delivered += len(batch)
    return delivered


def check(args: argparse.Namespace) -> int:
    config = load_json(CONFIG, {})
    if not isinstance(config.get("sites"), list): raise ValueError("magazalar.json içindeki sites bir liste olmalı")
    config, changed = ensure_store_ids(config)
    if changed: atomic_json(CONFIG, config)
    settings = merge_defaults(load_json(SETTINGS, {}), DEFAULT_SETTINGS)
    state = migrate_state(load_json(STATE, {}), config)
    sites = [site for site in config["sites"] if isinstance(site, dict) and site.get("enabled", True)]
    if args.status:
        print(f"Etkin site: {len(sites)} | Teslimat kuyruğu: {len(state['delivery_queue'])}")
        for site in sites:
            record = state["stores"].get(stable_store_id(site), {})
            print(f"{site.get('name', '?')}: {record.get('count', 'henüz taranmadı')} ürün, {record.get('checked', '-')}")
        return 0
    if args.test_bildirim:
        if not telegram_batch([{ "type": "TEST", "store_name": "Test", "product_name": "Bildirim bağlantısı çalışıyor", "url": "https://example.com"}]):
            print("Önce STREETWEAR_BOT_TOKEN ve STREETWEAR_CHAT_ID ayarla"); return 2
        print("Test bildirimi gönderildi"); return 0
    failures, discovered = [], []
    maximum, ratio = int(settings["scan"].get("max_products_per_store", 15000)), float(settings["scan"].get("minimum_catalog_ratio", .25))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, int(settings["scan"].get("max_workers", 4))))) as pool:
        jobs = {pool.submit(scan, site): site for site in sites}
        for future in concurrent.futures.as_completed(jobs):
            site, store_id = jobs[future], stable_store_id(jobs[future]); name = site.get("name", "Adsız mağaza")
            try:
                current, record = future.result(), state["stores"].get(store_id)
                if len(current) > int(site.get("max_products", maximum)): raise ValueError(f"Katalog güvenlik sınırını aştı ({len(current)} ürün)")
                previous = record.get("current_products", {}) if record else {}
                if previous and len(current) < max(1, int(record.get("count", len(previous)) * ratio)): raise ValueError("Ürün sayısı şüpheli biçimde azaldı; önceki kayıt korundu")
                store = {"id": store_id, "name": name, "url": site["url"]}
                if not record: record = {**store, "known_products": {}, "current_products": {}, "transition_generations": {}, "baseline_pending": True}
                events = [] if record.get("baseline_pending") else compare_store(record, store, current)
                discovered.extend(events); merge_known(record, current, int(settings["scan"].get("known_products_limit", KNOWN_PRODUCTS_LIMIT)))
                state["stores"][store_id] = {**record, **store, "current_products": current, "count": len(current), "checked": now(), "last_error": None, "baseline_pending": False}
                print(f"{name}: {len(current)} ürün, {len(events)} yeni olay")
            except Exception as error:
                failures.append(name); record = state["stores"].setdefault(store_id, {"id": store_id, "name": name, "url": site["url"], "known_products": {}, "current_products": {}, "count": 0, "checked": "-", "transition_generations": {}, "baseline_pending": True})
                record["last_error"] = f"{now()}: {error}"; print(f"{name}: HATA: {error}", file=sys.stderr)
    record_events(state, discovered, settings, favorite_urls())
    if args.dry_run: print(f"Kuru çalışma: {len(state['delivery_queue'])} bildirim gönderilmeden bekletildi.")
    elif state["delivery_queue"]:
        try:
            if not os.getenv("STREETWEAR_BOT_TOKEN") or not os.getenv("STREETWEAR_CHAT_ID"): print(f"{len(state['delivery_queue'])} olay bulundu. Telegram ayarlanmadı; bildirim beklemede.")
            else: print(f"{deliver_batches(state, telegram_batch, lambda: atomic_json(STATE, state))} olay Telegram ile bildirildi")
        except Exception as error:
            atomic_json(STATE, state); print(f"Bildirim hatası: {error}; yalnızca teslim edilmemiş olaylar kuyrukta kaldı", file=sys.stderr); return 2
    atomic_json(STATE, state)
    if failures: print(f"Başarısız mağaza sayısı: {len(failures)}. Kayıtlar silinmedi."); return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Streetwear ürün, stok ve fiyat olaylarını kontrol et")
    parser.add_argument("--status", action="store_true"); parser.add_argument("--test-bildirim", action="store_true"); parser.add_argument("--dry-run", action="store_true")
    return check(parser.parse_args())


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as error: print(f"HATA: {error}", file=sys.stderr); raise SystemExit(2)