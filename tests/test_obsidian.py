# -*- coding: utf-8 -*-
"""Obsidian 同步的离线测试。"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from daily import default_profile, normalize_obsidian_folder, validate_profile
from obsidian import ObsidianError, USER_END, USER_START, sync_state


def make_profile(vault: Path) -> dict:
    profile = default_profile()
    profile["fields"] = [{
        "id": "f_ai",
        "name": "人工智能教育",
        "keywords": ["AI literacy"],
        "color": "#2F5D50",
        "weight": 1,
    }]
    profile["sources"] = ["openalex"]
    profile["obsidian_enabled"] = True
    profile["obsidian_vault"] = str(vault)
    profile["obsidian_folder"] = "知新"
    profile["obsidian_auto_sync"] = True
    return profile


def make_pick(score=4):
    return {
        "id": "doi:10.1234/obsidian.1",
        "doi": "10.1234/obsidian.1",
        "title": "AI literacy for teacher education: a mixed-methods study",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "author_line": "Ada Lovelace, Alan Turing",
        "journal": "Computers & Education",
        "year": 2026,
        "published": "2026-09-12",
        "abstract": "This study investigates AI literacy in teacher education with a mixed-methods design.",
        "scores": {
            "relevance": score, "expertise": 4, "reliability": 4,
            "novelty": 4, "inspiration": 4,
        },
        "score_note": "方法部分可以借鉴。",
        "read_status": "read",
        "scored_at": "2026-10-26T18:20:00+08:00",
        "fields": [{"id": "f_ai", "name": "人工智能教育", "color": "#2F5D50"}],
        "sources": ["openalex", "crossref"],
        "why": ["命中 AI literacy", "近期发表"],
        "signals": {k: "测试信号" for k in
                    ["relevance", "expertise", "reliability", "novelty", "inspiration"]},
        "oa_url": "https://example.org/paper.pdf",
    }


def make_state(vault: Path, score=4, include_unscored=False):
    profile = make_profile(vault)
    day = {
        "date": "2026-10-26",
        "picks": [make_pick(score)],
        "preference": {"focus": ["novelty", "inspiration"], "note": "想看新方法。"},
    }
    days = {"2026-10-26": day}
    if include_unscored:
        days["2026-10-27"] = {
            "date": "2026-10-27",
            "picks": [{**make_pick(4), "id": "doi:10.1234/pending", "doi": "10.1234/pending",
                       "scores": {}, "score_note": "", "read_status": "unread",
                       "scored_at": None, "title": "Pending paper without a score"}],
            "preference": None,
        }
    return {"profile": profile, "days": days}


class ObsidianTests(unittest.TestCase):
    def test_sync_writes_paper_daily_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result = sync_state(make_state(vault))
            self.assertTrue(result["ok"])
            self.assertEqual(result["papers"], 1)
            self.assertEqual(result["daily"], 1)
            unchanged = sync_state(make_state(vault))
            self.assertEqual(unchanged["papers"], 0)
            self.assertEqual(unchanged["daily"], 0)
            papers = list((vault / "知新" / "文献" / "2026").glob("*.md"))
            self.assertEqual(len(papers), 1)
            self.assertTrue((vault / "知新" / "日报" / "2026-10-26.md").exists())
            self.assertTrue((vault / "知新" / "索引.md").exists())
            text = papers[0].read_text(encoding="utf-8")
            self.assertIn("zhixin: true", text)
            self.assertIn("rating: 4.0", text)
            self.assertIn("## 我的评分", text)
            self.assertIn("Google Scholar", text)
            self.assertIn("[[知新/日报/2026-10-26|2026-10-26 日报]]", text)
            self.assertIn(USER_START, text)
            daily = (vault / "知新" / "日报" / "2026-10-26.md").read_text(encoding="utf-8")
            self.assertIn("创新性、启发性", daily)
            self.assertIn("想看新方法", daily)

    def test_resync_preserves_user_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            sync_state(make_state(vault, score=4))
            paper = next((vault / "知新" / "文献" / "2026").glob("*.md"))
            text = paper.read_text(encoding="utf-8")
            text = re.sub(
                re.escape(USER_START) + r".*?" + re.escape(USER_END),
                USER_START + "\n这是我自己的延伸思考。\n" + USER_END,
                text,
                flags=re.S,
            )
            paper.write_text(text, encoding="utf-8")
            sync_state(make_state(vault, score=5))
            updated = paper.read_text(encoding="utf-8")
            self.assertIn("这是我自己的延伸思考。", updated)
            self.assertIn("rating: 4.2", updated)

    def test_unscored_paper_gets_daily_but_not_paper_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            sync_state(make_state(vault, include_unscored=True))
            papers = list((vault / "知新" / "文献").rglob("*.md"))
            self.assertEqual(len(papers), 1)
            self.assertTrue((vault / "知新" / "日报" / "2026-10-27.md").exists())
            index = (vault / "知新" / "索引.md").read_text(encoding="utf-8")
            self.assertNotIn("Pending paper without a score", index)

    def test_refuses_to_overwrite_foreign_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            target = vault / "知新" / "索引.md"
            target.parent.mkdir(parents=True)
            target.write_text("my own notes", encoding="utf-8")
            with self.assertRaises(ObsidianError):
                sync_state(make_state(vault))

    def test_requires_enabled_and_valid_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = make_state(Path(tmp))
            state["profile"]["obsidian_enabled"] = False
            with self.assertRaises(ObsidianError):
                sync_state(state)
            with self.assertRaises(ValueError):
                validate_profile({
                    "fields": [{"name": "AI", "keywords": "AI literacy"}],
                    "sources": ["openalex"],
                    "obsidian_enabled": True,
                    "obsidian_vault": str(Path(tmp) / "missing"),
                }, default_profile())

    def test_folder_traversal_rejected(self):
        with self.assertRaises(ValueError):
            normalize_obsidian_folder("../outside")
        self.assertEqual(normalize_obsidian_folder("知新/文献"), "知新/文献")


if __name__ == "__main__":
    unittest.main()
