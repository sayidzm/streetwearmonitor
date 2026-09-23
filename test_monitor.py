"""Standard library tests: `py -3 -m unittest -v test_monitor.py`."""
import unittest
from unittest.mock import patch

import monitor
from scanners.shopify import ShopifyScanner

STORE = {"id": "store_demo", "name": "Demo", "url": "https://example.com"}


def item(product_id="shopify:1", *, url="https://example.com/products/a", available=True, price=100,
         image="https://cdn.example/a.jpg", variants=None):
    return {"id": product_id, "name": "Ürün A", "url": url, "image": image, "price": price,
            "compare_at_price": None, "currency": "EUR", "available": available, "variants": variants or {}}


def variant(variant_id="shopify:11", *, available=True, price=100):
    return {"id": variant_id, "title": "Small", "available": available, "price": price, "compare_at_price": None}


def record(current, known=None):
    return {"current_products": current, "known_products": known if known is not None else dict(current), "transition_generations": {}}


class MonitorTests(unittest.TestCase):
    def setUp(self): self.settings = monitor.merge_defaults({}, monitor.DEFAULT_SETTINGS)

    def test_legacy_migration_keeps_urls_and_is_silent_baseline(self):
        config = {"sites": [{"id": "store_demo", "name": "Demo", "url": "https://example.com"}]}
        state = monitor.migrate_state({"stores": {"Demo": {"seen": ["https://example.com/products/a/"], "count": 1}}}, config)
        self.assertEqual(state["schema_version"], 4)
        self.assertTrue(state["stores"]["store_demo"]["baseline_pending"])
        self.assertIn("url:https://example.com/products/a", state["stores"]["store_demo"]["known_products"])

    def test_v3_to_v4_migration_is_idempotent_and_keeps_products(self):
        raw = {"version": 3, "stores": {"store_demo": {"products": {"shopify:1": item()}, "baseline_pending": False}}, "pending": [{"id": "old", "url": "https://example.com/products/a"}]}
        first = monitor.migrate_state(raw, {"sites": [STORE]})
        second = monitor.migrate_state(first, {"sites": [STORE]})
        self.assertEqual(first, second)
        self.assertTrue(first["stores"]["store_demo"]["baseline_pending"])
        self.assertEqual(first["delivery_queue"], [])
        self.assertEqual(first["legacy_pending"][0]["delivery_status"], "legacy_not_queued")

    def test_store_rename_preserves_id_keyed_state(self):
        raw = {"schema_version": 4, "stores": {"store_demo": {"id": "store_demo", "name": "Old", "current_products": {"shopify:1": item()}}}}
        self.assertIn("store_demo", monitor.migrate_state(raw, {"sites": [STORE]})["stores"])

    def test_shopify_product_url_change_is_not_new_product(self):
        previous = {"shopify:1": item(url="https://example.com/products/old")}
        current = {"shopify:1": item(url="https://example.com/products/new")}
        self.assertEqual(monitor.compare_products(STORE, current, previous), [])

    def test_product_restock_and_sold_out(self):
        restock = monitor.compare_products(STORE, {"shopify:1": item(available=True)}, {"shopify:1": item(available=False)})
        sold = monitor.compare_products(STORE, {"shopify:1": item(available=False)}, {"shopify:1": item(available=True)})
        self.assertEqual([event["type"] for event in restock], ["RESTOCK"])
        self.assertEqual([event["type"] for event in sold], ["SOLD_OUT"])

    def test_variant_restock_and_sold_out(self):
        old = {"shopify:1": item(variants={"shopify:11": variant(available=False)})}
        current = {"shopify:1": item(variants={"shopify:11": variant(available=True)})}
        self.assertIn("VARIANT_RESTOCK", [event["type"] for event in monitor.compare_products(STORE, current, old)])
        old["shopify:1"], current["shopify:1"] = item(variants={"shopify:11": variant(available=True)}), item(variants={"shopify:11": variant(available=False)})
        self.assertIn("VARIANT_SOLD_OUT", [event["type"] for event in monitor.compare_products(STORE, current, old)])

    def test_price_drop_and_increase_include_percent(self):
        previous = {"shopify:1": item(price=100)}
        down = monitor.compare_products(STORE, {"shopify:1": item(price=80)}, previous)[0]
        up = monitor.compare_products(STORE, {"shopify:1": item(price=120)}, previous)[0]
        self.assertEqual((down["type"], down["old_price"], down["new_price"], down["change_percent"]), ("PRICE_DROP", 100, 80, -20.0))
        self.assertEqual((up["type"], up["change_percent"]), ("PRICE_INCREASE", 20.0))

    def test_repeated_variant_restock_gets_new_occurrence_id(self):
        old = {"shopify:1": item(variants={"shopify:11": variant(available=False)})}
        state = record(old)
        up = {"shopify:1": item(variants={"shopify:11": variant(available=True)})}
        first = [event for event in monitor.compare_store(state, STORE, up) if event["type"] == "VARIANT_RESTOCK"][0]
        state["current_products"] = up
        down = {"shopify:1": item(variants={"shopify:11": variant(available=False)})}
        monitor.compare_store(state, STORE, down); state["current_products"] = down
        second = [event for event in monitor.compare_store(state, STORE, up) if event["type"] == "VARIANT_RESTOCK"][0]
        self.assertEqual((first["occurrence"]["generation"], second["occurrence"]["generation"]), (1, 2))
        self.assertNotEqual(first["id"], second["id"])

    def test_repeated_product_restock_gets_new_occurrence_id(self):
        down, up = {"shopify:1": item(available=False)}, {"shopify:1": item(available=True)}
        state = record(down)
        first = [event for event in monitor.compare_store(state, STORE, up) if event["type"] == "RESTOCK"][0]
        state["current_products"] = up; monitor.compare_store(state, STORE, down); state["current_products"] = down
        second = [event for event in monitor.compare_store(state, STORE, up) if event["type"] == "RESTOCK"][0]
        self.assertNotEqual(first["id"], second["id"])

    def test_retry_same_scan_emits_no_duplicate(self):
        old, up = {"shopify:1": item(available=False)}, {"shopify:1": item(available=True)}
        state = record(old)
        self.assertTrue(monitor.compare_store(state, STORE, up))
        state["current_products"] = up
        self.assertEqual(monitor.compare_store(state, STORE, up), [])

    def test_filtered_event_is_history_not_delivery_queue(self):
        state = {"event_history": [], "delivery_queue": []}
        settings = monitor.merge_defaults({"telegram": {"notify_stock": False}}, monitor.DEFAULT_SETTINGS)
        monitor.record_events(state, [monitor.event("RESTOCK", STORE, item(), before=False, after=True)], settings, set())
        self.assertEqual(len(state["event_history"]), 1)
        self.assertEqual(state["event_history"][0]["delivery_status"], "skipped_policy")
        self.assertEqual(state["delivery_queue"], [])

    def test_notification_setting_change_does_not_enqueue_old_skipped_event(self):
        state = {"event_history": [], "delivery_queue": []}
        muted = monitor.merge_defaults({"telegram": {"notify_price_increases": False}}, monitor.DEFAULT_SETTINGS)
        monitor.record_events(state, [monitor.event("PRICE_INCREASE", STORE, item(), before=100, after=120)], muted, set())
        enabled = monitor.merge_defaults({"telegram": {"notify_price_increases": True}}, monitor.DEFAULT_SETTINGS)
        self.assertEqual(state["delivery_queue"], [])
        self.assertEqual(state["event_history"][0]["delivery_status"], "skipped_policy")
        self.assertTrue(monitor.allowed(monitor.event("PRICE_INCREASE", STORE, item(), before=100, after=120), enabled, set()))

    def test_partial_batch_failure_delivers_only_successful_batches(self):
        events = [monitor.event("NEW_PRODUCT", STORE, item(product_id=f"shopify:{n}"), occurrence=n) for n in range(20)]
        state = {"event_history": [dict(event, delivery_status="queued") for event in events], "delivery_queue": list(events), "delivered_event_ids": []}
        calls, saves = [], []
        def sender(batch):
            calls.append([event["id"] for event in batch])
            if len(calls) == 3: raise RuntimeError("third batch failed")
            return True
        with self.assertRaisesRegex(RuntimeError, "third"):
            monitor.deliver_batches(state, sender, lambda: saves.append(list(state["delivery_queue"])))
        self.assertEqual((len(state["delivery_queue"]), len(state["delivered_event_ids"]), len(saves)), (4, 16, 2))
        retried = []
        monitor.deliver_batches(state, lambda batch: retried.append([event["id"] for event in batch]) or True, lambda: None)
        self.assertEqual(len(retried), 1)
        self.assertEqual(len(retried[0]), 4)

    def test_disappearance_then_return_is_not_new_product(self):
        product = item()
        state = record({"shopify:1": product})
        state["current_products"] = {}  # a successful scan no longer lists it
        events = monitor.compare_store(state, STORE, {"shopify:1": product})
        self.assertEqual([event["type"] for event in events], ["PRODUCT_RETURNED"])

    def test_image_change_alone_is_not_an_event(self):
        self.assertEqual(monitor.compare_products(STORE, {"shopify:1": item(image="https://cdn/new.jpg")}, {"shopify:1": item(image="https://cdn/old.jpg")}), [])

    def test_sitemap_requires_product_path(self):
        from scanners.sitemap import PRODUCT_PATHS
        self.assertIsNotNone(PRODUCT_PATHS.search("/products/hoodie")); self.assertIsNone(PRODUCT_PATHS.search("/brand/lookbook/item"))

    def test_shopify_currency_config_detected_and_unknown(self):
        payload = {"currency": "usd", "products": [{"id": 7, "handle": "p", "title": "P", "images": [], "variants": [{"id": 9, "title": "S", "price": "10", "available": True}]}]}
        with patch.object(ShopifyScanner, "json", return_value=payload):
            detected = ShopifyScanner().scan({"url": "https://example.com"})["shopify:7"]
            explicit = ShopifyScanner().scan({"url": "https://example.com", "currency": "eur"})["shopify:7"]
        unknown_payload = {"products": payload["products"]}
        with patch.object(ShopifyScanner, "json", return_value=unknown_payload): unknown = ShopifyScanner().scan({"url": "https://example.com"})["shopify:7"]
        self.assertEqual((detected["currency"], explicit["currency"], unknown["currency"]), ("USD", "EUR", "UNKNOWN"))

    def test_recording_same_event_twice_does_not_duplicate_history_or_queue(self):
        state = {"event_history": [], "delivery_queue": []}
        created = monitor.event("NEW_PRODUCT", STORE, item())
        monitor.record_events(state, [created], self.settings, set())
        monitor.record_events(state, [created], self.settings, set())
        self.assertEqual((len(state["event_history"]), len(state["delivery_queue"])), (1, 1))

if __name__ == "__main__": unittest.main()