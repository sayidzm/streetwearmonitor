#!/usr/bin/env python3
"""Streetwear yeni ürün ve fiyat düşüşü takipçisi (Python 3.10+, bağımlılıksız)."""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
CONFIG, STATE = ROOT / "magazalar.json", ROOT / "veri.json"
SETTINGS, FAVORITES = ROOT / "ayarlar.json", ROOT / "favoriler.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StreetwearTakip/2.0; +local-personal-use)"}
PRODUCT_PATHS = re.compile(r"/(?:products?|urun(?:ler)?|p)/[^/?#]+", re.I)
SKIP_PATHS = re.compile(r"/(?:collections?|categories?|kategori|blog|pages?|search|cart|account|tags?)/", re.I)
STATE_VERSION = 2
DEFAULT_SETTINGS: dict[str, Any] = {
    "telegram": {"notify_new": True, "notify_price_drops": True, "only_favorites": False,
                 "brands": [], "include_keywords": [], "exclude_keywords": [], "minimum_discount_percent": 0},
    "scan": {"max_workers": 4, "max_products_per_store": 15000, "minimum_catalog_ratio": 0.25},
}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fetch(url: str, limit: int = 8_000_000) -> bytes:
    with urlopen(Request(url, headers=HEADERS), timeout=25) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Yanıt çok büyük")
        if response.headers.get("Content-Encoding", "").lower() == "gzip" or url.endswith(".gz"):
            data = gzip.decompress(data)
        return data


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


def normalize(url: str) -> str:
    parsed = urlparse(url.strip())
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower() or "https", parsed.netloc.lower(), path, "", "", ""))


def same_host(url: str, base: str) -> bool:
    return (urlparse(url).hostname or "").removeprefix("www.").lower() == (urlparse(base).hostname or "").removeprefix("www.").lower()


def title_from_url(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]", " ", slug).strip().title() or "İsimsiz ürün"


def price_value(raw: Any) -> float | None:
    try:
        value = float(str(raw).replace(",", "."))
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def product(name: str, url: str, price: Any = None, compare_at_price: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": str(name or title_from_url(url)).strip()[:300], "url": normalize(url)}
    current, old = price_value(price), price_value(compare_at_price)
    if current is not None:
        item["price"] = current
    if current is not None and old is not None and old > current:
        item["compare_at_price"] = old
    return item


def shopify(base: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for page in range(1, 31):
        payload = json.loads(fetch(urljoin(base.rstrip("/") + "/", f"products.json?limit=250&page={page}")))
        products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(products, list):
            raise ValueError("Shopify ürün yanıtı geçerli değil")
        for item in products:
            if not isinstance(item, dict) or not item.get("handle"):
                continue
            url = urljoin(base.rstrip("/") + "/", "products/" + quote(str(item["handle"]), safe="-"))
            variants = item.get("variants") if isinstance(item.get("variants"), list) else []
            prices = [price_value(v.get("price")) for v in variants if isinstance(v, dict)]
            compares = [price_value(v.get("compare_at_price")) for v in variants if isinstance(v, dict)]
            found[normalize(url)] = product(item.get("title", ""), url, min((p for p in prices if p is not None), default=None), min((p for p in compares if p is not None), default=None))
        if len(products) < 250:
            return found
    raise ValueError("Shopify sayfa sınırı aşıldı; eksik katalog kaydedilmedi")


def sitemap(base: str) -> dict[str, dict[str, Any]]:
    queue, visited, found = [urljoin(base.rstrip("/") + "/", "sitemap.xml")], set(), {}
    while queue:
        if len(visited) >= 80:
            raise ValueError("Sitemap sınırı aşıldı; eksik katalog kaydedilmedi")
        url = queue.pop(0)
        if url in visited or not same_host(url, base):
            continue
        visited.add(url)
        root = ET.fromstring(fetch(url))
        if root.tag.endswith("sitemapindex"):
            for loc in root.findall(".//{*}sitemap/{*}loc"):
                link = (loc.text or "").strip()
                if link and same_host(link, base) and not re.search(r"(blog|article|page|category|collection|image)", link, re.I):
                    queue.append(link)
            continue
        for loc in root.findall(".//{*}url/{*}loc"):
            link = (loc.text or "").strip()
            path = urlparse(link).path
            if not link or not same_host(link, base) or SKIP_PATHS.search(path) or not PRODUCT_PATHS.search(path):
                continue
            link = normalize(link)
            found[link] = product(title_from_url(link), link)
    if not found:
        raise ValueError("Sitemap içinde güvenilir ürün bağlantısı yok")
    return found


def scan(site: dict[str, Any]) -> dict[str, dict[str, Any]]:
    base, method = site["url"], site.get("method", "auto")
    if method not in {"auto", "shopify", "sitemap"}:
        raise ValueError("Bilinmeyen tarama yöntemi")
    if method in {"auto", "shopify"}:
        try:
            result = shopify(base)
            if result:
                return result
            raise ValueError("Shopify ürün listesi boş")
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
            if method == "shopify":
                raise
    return sitemap(base)


def migrate_state(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    stores: dict[str, Any] = {}
    old_stores = raw.get("stores", {}) if isinstance(raw.get("stores"), dict) else {}
    for name, old in old_stores.items():
        old = old if isinstance(old, dict) else {}
        products = old.get("products") if isinstance(old.get("products"), dict) else {}
        if not products:
            products = {normalize(url): product(title_from_url(url), url) for url in old.get("seen", []) if isinstance(url, str)}
        stores[name] = {"products": products, "count": int(old.get("count", len(products))), "checked": old.get("checked", "-"), "last_error": old.get("last_error"),
                        "baseline_pending": old.get("baseline_pending", raw.get("version") != STATE_VERSION)}
    # URL-only v1 records cannot reliably be compared with richer v2 product records.
    # The first v2 scan is therefore a silent baseline, never a notification flood.
    pending: list[dict[str, Any]] = []
    for item in raw.get("pending", []) if isinstance(raw.get("pending"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        item = dict(item)
        item.setdefault("kind", "new")
        item.setdefault("name", title_from_url(item["url"]))
        item.setdefault("brand", "Bilinmeyen mağaza")
        item.setdefault("id", event_id(item["kind"], item["brand"], item))
        pending.append(item)
    return {"version": STATE_VERSION, "_legacy": raw.get("version") != STATE_VERSION, "stores": stores,
            "pending": pending,
            "sent": raw.get("sent", {}) if isinstance(raw.get("sent"), dict) else {}}


def favorite_urls() -> set[str]:
    raw = load_json(FAVORITES, [])
    values = raw.get("favorites", []) if isinstance(raw, dict) else raw
    return {normalize(item["url"] if isinstance(item, dict) else item) for item in values if isinstance(item, str) or isinstance(item, dict) and isinstance(item.get("url"), str)}


def event_id(kind: str, brand: str, item: dict[str, Any], old_price: float | None = None) -> str:
    suffix = f":{old_price:.2f}->{item.get('price'):.2f}" if kind == "price_drop" and old_price is not None and isinstance(item.get("price"), (int, float)) else ""
    return f"{kind}:{brand}:{item['url']}{suffix}"


def make_events(brand: str, current: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]], sent: dict[str, str]) -> list[dict[str, Any]]:
    events = []
    for url, item in current.items():
        old = previous.get(url)
        if old is None:
            kind, old_price = "new", None
        else:
            old_price, new_price = price_value(old.get("price")), price_value(item.get("price"))
            if old_price is None or new_price is None or new_price >= old_price:
                continue
            kind = "price_drop"
        identity = event_id(kind, brand, item, old_price)
        if identity not in sent:
            event = {"id": identity, "kind": kind, "brand": brand, **item}
            if old_price is not None:
                event["old_price"] = old_price
            events.append(event)
    return events


def allowed(event: dict[str, Any], settings: dict[str, Any], favorites: set[str]) -> bool:
    rules = settings["telegram"]
    if event["kind"] == "new" and not rules["notify_new"] or event["kind"] == "price_drop" and not rules["notify_price_drops"]:
        return False
    if rules["only_favorites"] and normalize(event["url"]) not in favorites:
        return False
    brands = {str(value).casefold() for value in rules.get("brands", [])}
    if brands and event["brand"].casefold() not in brands:
        return False
    text = f"{event['brand']} {event['name']}".casefold()
    include = [str(value).casefold() for value in rules.get("include_keywords", []) if str(value).strip()]
    exclude = [str(value).casefold() for value in rules.get("exclude_keywords", []) if str(value).strip()]
    if include and not any(value in text for value in include) or any(value in text for value in exclude):
        return False
    return not (event["kind"] == "price_drop" and event.get("old_price") and (1 - event["price"] / event["old_price"]) * 100 < float(rules.get("minimum_discount_percent", 0)))


def money(value: Any) -> str:
    return f"₺{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def telegram(items: list[dict[str, Any]]) -> bool:
    token, chat_id = os.getenv("STREETWEAR_BOT_TOKEN"), os.getenv("STREETWEAR_CHAT_ID")
    if not token or not chat_id:
        return False
    for offset in range(0, len(items), 8):
        lines = ["🔔 Streetwear Takip"]
        for item in items[offset:offset + 8]:
            details = f" — {money(item['old_price'])} → {money(item['price'])}" if item["kind"] == "price_drop" else ""
            lines.append(f"{'🆕' if item['kind'] == 'new' else '💸'} {html.escape(item['brand'])}: <a href=\"{html.escape(item['url'], quote=True)}\">{html.escape(item['name'][:110])}</a>{details}")
        payload = json.dumps({"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
        result = json.loads(urlopen(Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers={"Content-Type": "application/json"}), timeout=25).read())
        if not result.get("ok"):
            raise ValueError("Telegram bildirimi reddetti")
    return True


def check(args: argparse.Namespace) -> int:
    config = load_json(CONFIG, {})
    if not isinstance(config.get("sites"), list):
        raise ValueError("magazalar.json içindeki sites bir liste olmalı")
    settings, state = merge_defaults(load_json(SETTINGS, {}), DEFAULT_SETTINGS), migrate_state(load_json(STATE, {}))
    legacy_baseline = state.pop("_legacy", False)
    sites = [site for site in config["sites"] if isinstance(site, dict) and site.get("enabled", True)]
    if args.status:
        print(f"Etkin site: {len(sites)} | Adresi beklenen: {len(config.get('waiting_for_url', []))} | Bekleyen bildirim: {len(state['pending'])}")
        for site in sites:
            record = state["stores"].get(site.get("name"), {})
            print(f"{site.get('name', '?')}: {record.get('count', 'henüz taranmadı')} ürün, {record.get('checked', '-')}")
        return 0
    if args.test_bildirim:
        if not telegram([{"kind": "new", "brand": "Test", "name": "Bildirim bağlantısı çalışıyor", "url": "https://example.com"}]):
            print("Önce STREETWEAR_BOT_TOKEN ve STREETWEAR_CHAT_ID ayarla")
            return 2
        print("Test bildirimi gönderildi")
        return 0
    max_products, ratio = int(settings["scan"].get("max_products_per_store", 15000)), float(settings["scan"].get("minimum_catalog_ratio", .25))
    failures, discovered = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, int(settings["scan"].get("max_workers", 4))))) as pool:
        jobs = {pool.submit(scan, site): site for site in sites}
        for future in concurrent.futures.as_completed(jobs):
            site, name = jobs[future], jobs[future].get("name", "Adsız mağaza")
            try:
                current, old_record = future.result(), state["stores"].get(name)
                if len(current) > int(site.get("max_products", max_products)):
                    raise ValueError(f"Katalog güvenlik sınırını aştı ({len(current)} ürün)")
                previous = old_record.get("products", {}) if old_record else {}
                if previous and len(current) < max(1, int(old_record.get("count", len(previous)) * ratio)):
                    raise ValueError("Ürün sayısı şüpheli biçimde azaldı; önceki kayıt korundu")
                is_baseline = legacy_baseline or bool(old_record and old_record.get("baseline_pending"))
                events = make_events(name, current, previous, state["sent"]) if old_record and not is_baseline else []
                discovered.extend(events)
                state["stores"][name] = {"products": current, "count": len(current), "checked": now(), "last_error": None, "baseline_pending": False}
                print(f"{name}: {len(current)} ürün, {len(events)} yeni olay" if old_record else f"{name}: ilk güvenilir tarama, {len(current)} ürün kaydedildi")
            except Exception as error:
                failures.append(name)
                record = state["stores"].setdefault(name, {"products": {}, "count": 0, "checked": "-", "baseline_pending": legacy_baseline})
                record["last_error"] = f"{now()}: {error}"
                print(f"{name}: HATA: {error}", file=sys.stderr)
    queued = {item.get("id") for item in state["pending"] if isinstance(item, dict)}
    state["pending"].extend(item for item in discovered if item["id"] not in queued)
    to_send = [item for item in state["pending"] if isinstance(item, dict) and allowed(item, settings, favorite_urls())]
    if args.dry_run:
        print(f"Kuru çalışma: {len(to_send)} Telegram bildirimi gönderilmeden listelendi.")
    elif to_send:
        try:
            if telegram(to_send):
                sent_ids = {item["id"] for item in to_send}
                state["pending"] = [item for item in state["pending"] if item.get("id") not in sent_ids]
                state["sent"].update({item_id: now() for item_id in sent_ids})
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
    parser = argparse.ArgumentParser(description="Yeni streetwear ürünlerini ve fiyat düşüşlerini kontrol et")
    parser.add_argument("--status", action="store_true", help="Mağazalar, son tarama ve bekleyen bildirimler")
    parser.add_argument("--test-bildirim", action="store_true", help="Telegram bağlantısını dene")
    parser.add_argument("--dry-run", action="store_true", help="Tarar, Telegram'a göndermez")
    return check(parser.parse_args())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"HATA: {error}", file=sys.stderr)
        raise SystemExit(2)
