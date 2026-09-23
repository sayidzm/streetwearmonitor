"""Standart kütüphane testleri: `py -3 -m unittest -v test_monitor.py`."""
import unittest

import monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.settings = monitor.merge_defaults({}, monitor.DEFAULT_SETTINGS)

    def test_v1_migration_keeps_seen_urls_and_marks_silent_baseline(self):
        state = monitor.migrate_state({"stores": {"Demo": {"seen": ["https://example.com/products/a/"], "count": 1}}})
        self.assertTrue(state["_legacy"])
        self.assertIn("https://example.com/products/a", state["stores"]["Demo"]["products"])
        self.assertTrue(state["stores"]["Demo"]["baseline_pending"])

    def test_price_drop_is_an_event_but_price_increase_is_not(self):
        previous = {"https://example.com/products/a": {"name": "A", "url": "https://example.com/products/a", "price": 100}}
        dropped = {"https://example.com/products/a": {"name": "A", "url": "https://example.com/products/a", "price": 80}}
        raised = {"https://example.com/products/a": {"name": "A", "url": "https://example.com/products/a", "price": 120}}
        self.assertEqual(monitor.make_events("Demo", dropped, previous, {})[0]["kind"], "price_drop")
        self.assertEqual(monitor.make_events("Demo", raised, previous, {}), [])

    def test_sent_event_is_not_repeated(self):
        current = {"https://example.com/products/a": {"name": "A", "url": "https://example.com/products/a"}}
        identity = monitor.event_id("new", "Demo", current["https://example.com/products/a"])
        self.assertEqual(monitor.make_events("Demo", current, {}, {identity: "2026-01-01 00:00:00"}), [])

    def test_filters_apply_brand_keyword_discount_and_favorite(self):
        event = {"kind": "price_drop", "brand": "Demo", "name": "Oversize Hoodie", "url": "https://example.com/products/a", "price": 70, "old_price": 100}
        rules = self.settings["telegram"]
        rules.update({"brands": ["Demo"], "include_keywords": ["hoodie"], "exclude_keywords": ["kids"], "minimum_discount_percent": 25, "only_favorites": True})
        self.assertTrue(monitor.allowed(event, self.settings, {event["url"]}))
        self.assertFalse(monitor.allowed(event, self.settings, set()))

    def test_sitemap_requires_product_path(self):
        self.assertIsNotNone(monitor.PRODUCT_PATHS.search("/products/hoodie"))
        self.assertIsNone(monitor.PRODUCT_PATHS.search("/brand/lookbook/item"))


if __name__ == "__main__":
    unittest.main()
