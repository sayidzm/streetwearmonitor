"""Yalnızca güvenilir ürün URL desenleri için sitemap adapterı."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from .base import Scanner, fetch, normalize, same_host

PRODUCT_PATHS = re.compile(r"/(?:products?|urun(?:ler)?|p)/[^/?#]+", re.I)
SKIP_PATHS = re.compile(r"/(?:collections?|categories?|kategori|blog|pages?|search|cart|account|tags?)/", re.I)


def title_from_url(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]", " ", slug).strip().title() or "İsimsiz ürün"


class SitemapScanner(Scanner):
    def scan(self, site: dict[str, Any]) -> dict[str, dict[str, Any]]:
        base = site["url"]
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
                product_id = "url:" + link
                found[product_id] = {"id": product_id, "name": title_from_url(link), "url": link,
                                     "image": None, "price": None, "compare_at_price": None,
                                     "currency": None, "available": None, "variants": {}}
        if not found:
            raise ValueError("Sitemap içinde güvenilir ürün bağlantısı yok")
        return found
