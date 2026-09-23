"""Yeni scanner'ların uyguladığı küçük ve bağımlılıksız ortak sözleşme."""
from __future__ import annotations

from abc import ABC, abstractmethod
import gzip
import json
import re
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StreetwearTakip/3.0; +local-personal-use)"}


def fetch(url: str, limit: int = 8_000_000) -> bytes:
    with urlopen(Request(url, headers=HEADERS), timeout=25) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Yanıt çok büyük")
        if response.headers.get("Content-Encoding", "").lower() == "gzip" or url.endswith(".gz"):
            data = gzip.decompress(data)
        return data


def normalize(url: str) -> str:
    parsed = urlparse(url.strip())
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower() or "https", parsed.netloc.lower(), path, "", "", ""))


def same_host(url: str, base: str) -> bool:
    return (urlparse(url).hostname or "").removeprefix("www.").lower() == (urlparse(base).hostname or "").removeprefix("www.").lower()


def price_value(raw: Any) -> float | None:
    try:
        value = float(str(raw).replace(",", "."))
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


class Scanner(ABC):
    """Bir mağazadan `product_id -> ürün` sözlüğü üretir."""

    @abstractmethod
    def scan(self, site: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def json(url: str) -> Any:
        return json.loads(fetch(url))
