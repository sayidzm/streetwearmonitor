"""Standart kütüphane testleri: `py -3 -m unittest -v test_monitor.py`."""
import unittest
from unittest.mock import patch

import monitor
from scanners.shopify import ShopifyScanner


STORE = {"id": "store_demo", "name": "Yeni Mağaza", "url": "https://example.com"}


def item(product_id="shopify:1", *, url="https://example.com/products/a", available=True,
         price=100, image="https://cdn.example/a.jpg", variants=None):
    return {
        "id": product_id, "name": "Ürün A", "url": url, "image": image,
        "price": price, "compare_at_price": None, "currency": "EUR",
        "available": available, "variants": variants or {},
    }


def variant(variant_id="shopify:11", *, available=True, price=100):
    return {"id": variant_id, "title": "Small", "available": available,
            "price": price, "compare_at_price": None}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.settings = monitor.merge_defaults({}, monitor.DEFAULT_SETTINGS)

    def test_legacy_migration_keeps_urls_and_is_silent_baseline(self):
        config = {"sites": [{"id": "store_demo", "name": "Demo", "url": "https://example.com"}]}
        state = monitor.migrate_state({"stores": {"Demo": {"seen": ["https://example.com/products/a/"], "count": 1}}}, config)
        record = state["stores"]["store_demo"]
        self.assertTrue(record["baseline_pending"])
        self.assertIn("url:https://example.com/products/a", record["products"])

    def test_store_rename_preserves_id_keyed_state(self):
        raw = {"version": 3, "stores": {"store_demo": {"id": "store_demo", "name": "Eski Mağaza", "products": {"shopify:1": item()}}}}
        config = {"sites": [{"id": "store_demo", "name": "Yeni Mağaza", "url": "https://example.com"}]}
        self.assertIn("store_demo", monitor.migrate_state(raw, config)["stores"])

    def test_shopify_product_url_change_is_not_new_product(self):
        previous = {"shopify:1": item(url="https://example.com/products/old-handle")}
        current = {"shopify:1": item(url="https://example.com/products/new-handle")}
        self.assertEqual(monitor.compare_products(STORE, current, previous), [])

    def test_product_restock_and_sold_out(self):
        restock = monitor.compare_products(STORE, {"shopify:1": item(available=True)}, {"shopify:1": item(available=False)})
        sold_out = monitor.compare_products(STORE, {"shopify:1": item(available=False)}, {"shopify:1": item(available=True)})
        self.assertEqual([event["type"] for event in restock], ["RESTOCK"])
        self.assertEqual([event["type"] for event in sold_out], ["SOLD_OUT"])

    def test_variant_restock_and_sold_out(self):
        old = {"shopify:1": item(variants={"shopify:11": variant(available=False)})}
        current = {"shopify:1": item(variants={"shopify:11": variant(available=True)})}
        self.assertIn("VARIANT_RESTOCK", [event["type"] for event in monitor.compare_products(STORE, current, old)])
        old["shopify:1"] = item(variants={"shopify:11": variant(available=True)})
        current["shopify:1"] = item(variants={"shopify:11": variant(available=False)})
        self.assertIn("VARIANT_SOLD_OUT", [event["type"] for event in monitor.compare_products(STORE, current, old)])

    def test_price_drop_and_increase_include_percent(self):
        previous = {"shopify:1": item(price=100)}
        down = monitor.compare_products(STORE, {"shopify:1": item(price=80)}, previous)[0]
        up = monitor.compare_products(STORE, {"shopify:1": item(price=120)}, previous)[0]
        self.assertEqual((down["type"], down["old_price"], down["new_price"], down["change_percent"]), ("PRICE_DROP", 100, 80, -20.0))
        self.assertEqual((up["type"], up["change_percent"]), ("PRICE_INCREASE", 20.0))

    def test_migration_baseline_does_not_emit_fake_restock(self):
        state = monitor.migrate_state({"stores": {"Demo": {"seen": ["https://example.com/products/a"]}}}, {"sites": [{"id": "store_demo", "name": "Demo", "url": "https://example.com"}]})
        self.assertTrue(state["stores"]["store_demo"]["baseline_pending"])

    def test_same_event_is_queued_once(self):
        state = {"pending": [], "event_history": [], "event_ids": []}
        event = monitor.event("NEW_PRODUCT", STORE, item())
        monitor.queue_events(state, [event]); monitor.queue_events(state, [event])
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual(len(state["event_history"]), 1)

    def test_image_change_alone_is_not_an_event(self):
        previous = {"shopify:1": item(image="https://cdn.example/old.jpg")}
        current = {"shopify:1": item(image="https://cdn.example/new.jpg")}
        self.assertEqual(monitor.compare_products(STORE, current, previous), [])

    def test_sitemap_requires_product_path(self):
        from scanners.sitemap import PRODUCT_PATHS
        self.assertIsNotNone(PRODUCT_PATHS.search("/products/hoodie"))
        self.assertIsNone(PRODUCT_PATHS.search("/brand/lookbook/item"))

    def test_shopify_adapter_keeps_product_variant_image_and_currency(self):
        payload = {"products": [{"id": 7, "handle": "renamed-product", "title": "Ürün", "images": [{"src": "https://cdn.example/main.jpg"}], "variants": [{"id": 9, "title": "Small", "price": "99.90", "compare_at_price": "129.90", "available": True}]}]}
        with patch.object(ShopifyScanner, "json", return_value=payload):
            product = ShopifyScanner().scan({"url": "https://example.com", "currency": "EUR"})["shopify:7"]
        self.assertEqual(product["url"], "https://example.com/products/renamed-product")
        self.assertEqual(product["image"], "https://cdn.example/main.jpg")
        self.assertEqual(product["currency"], "EUR")
        self.assertEqual(product["variants"]["shopify:9"]["title"], "Small")


if __name__ == "__main__":
    unittest.main()