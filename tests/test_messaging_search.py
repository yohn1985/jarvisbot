"""Conversation search + custom title (jarvis.messaging) — internals faked, no real state files."""
import unittest

from jarvis import messaging


class SearchAndTitleTests(unittest.TestCase):
    def setUp(self):
        self._rows = [
            {"conv": "c1", "conv_title": "WhatsApp fix", "text": "checking the integration", "ts": "2026-06-17T10:00:00", "kind": "message"},
            {"conv": "c2", "conv_title": "Ads", "text": "facebook campaign budget", "ts": "2026-06-17T11:00:00", "kind": "message"},
        ]
        self._meta = {}
        self._orig_all, self._orig_meta, self._orig_save = messaging._all, messaging._meta, messaging._save_meta
        messaging._all = lambda: list(self._rows)
        messaging._meta = lambda: dict(self._meta)
        messaging._save_meta = lambda m: self._meta.update(m)

    def tearDown(self):
        messaging._all, messaging._meta, messaging._save_meta = self._orig_all, self._orig_meta, self._orig_save

    def test_search_matches_title(self):
        self.assertEqual(messaging.search("whatsapp"), ["c1"])

    def test_search_matches_message_content(self):
        self.assertEqual(messaging.search("facebook"), ["c2"])

    def test_search_no_match(self):
        self.assertEqual(messaging.search("nonexistent-term"), [])

    def test_search_blank_returns_empty(self):
        self.assertEqual(messaging.search("   "), [])

    def test_set_title_stores_custom(self):
        messaging.set_title("c1", "My Renamed Thread")
        self.assertEqual(self._meta["c1"]["title"], "My Renamed Thread")

    def test_blank_title_clears(self):
        self._meta = {"c1": {"title": "Old"}}
        messaging.set_title("c1", "  ")
        self.assertNotIn("title", self._meta.get("c1", {}))

    def test_custom_title_overrides_auto(self):
        self._meta = {"c1": {"title": "Renamed"}}
        convs = {c["id"]: c["title"] for c in messaging.conversations()}
        self.assertEqual(convs["c1"], "Renamed")          # custom wins over conv_title "WhatsApp fix"
        self.assertEqual(convs["c2"], "Ads")              # untouched -> auto title


if __name__ == "__main__":
    unittest.main()
