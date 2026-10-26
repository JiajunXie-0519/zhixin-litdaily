# -*- coding: utf-8 -*-
"""Zotero 本地联动逻辑的离线测试。"""
from __future__ import annotations

import unittest

import zotero


def make_item(doi, title, journal, tags, status=None):
    return {
        "key": doi,
        "itemType": "journalArticle",
        "title": title,
        "doi": doi,
        "journal": journal,
        "date": "2026",
        "tags": tags,
        "status": status or zotero.status_for_tags(
            tags,
            {"/done", "done", "read", "已读"},
            {"/reading", "reading", "在读"},
            {"/unread", "unread", "未读"},
        ),
    }


class ZoteroTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            make_item("10.1/read", "Turbine blade cooling with transpiration", "Applied Thermal Engineering",
                      ["/done", "叶片冷却", "turbine"]),
            make_item("10.1/reading", "Aerothermal coupling in cooled turbine passages",
                      "Journal of Turbomachinery", ["/reading", "气热耦合"]),
            make_item("10.1/unread", "Unread paper about turbine cooling", "Energies", ["/unread", "叶片冷却"]),
            make_item("10.1/unknown", "Another paper", "Some Journal", ["实验"]),
        ]

    def test_status_mapping(self):
        self.assertEqual(self.items[0]["status"], "read")
        self.assertEqual(self.items[1]["status"], "reading")
        self.assertEqual(self.items[2]["status"], "unread")
        self.assertEqual(self.items[3]["status"], "unknown")

    def test_exclusion_modes(self):
        read = zotero.exclusion_set(self.items, "read")
        self.assertIn("10.1/read", read)
        self.assertIn("10.1/reading", read)
        self.assertNotIn("10.1/unread", read)
        all_items = zotero.exclusion_set(self.items, "all")
        self.assertIn("10.1/unread", all_items)
        self.assertEqual(zotero.exclusion_set(self.items, "off"), set())

    def test_preference_profile(self):
        profile = zotero.preference_profile(self.items)
        self.assertIn("叶片冷却", profile["terms"])
        self.assertIn("turbine", profile["terms"])
        self.assertNotIn("/done", profile["terms"])
        self.assertNotIn("/reading", profile["terms"])
        self.assertEqual(profile["journals"]["applied thermal engineering"], 1)

    def test_api_normalization(self):
        self.assertEqual(zotero.normalize_api("http://127.0.0.1:23119"),
                         "http://127.0.0.1:23119/api/users/0")
        with self.assertRaises(zotero.ZoteroError):
            zotero.normalize_api("https://example.com/api")

    def test_tag_defaults(self):
        read, reading, unread = zotero.tag_settings({})
        self.assertIn("/done", read)
        self.assertIn("已读", read)
        self.assertIn("/reading", reading)
        self.assertIn("/unread", unread)


if __name__ == "__main__":
    unittest.main()
