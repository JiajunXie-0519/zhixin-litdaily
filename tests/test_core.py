# -*- coding: utf-8 -*-
"""知新的离线单元测试：python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from daily import (
    DEFAULT_WEIGHTS,
    SOURCE_KEYS,
    Store,
    default_profile,
    effective_weights,
    institution_url,
    normalize_doi,
    public_profile,
    title_key,
    validate_profile,
)
from recommend import build_day, decorate_day, prior_exclusions, score_candidate, select_picks, total_of
from report import build_backup, build_csv, build_report_html
from sources import _merge_candidates, _smart_title


def sample_candidate(**overrides):
    candidate = {
        "id": "doi:10.1234/test.1",
        "doi": "10.1234/test.1",
        "title": "Artificial intelligence literacy for teacher education: a mixed-methods study",
        "journal": "Computers & Education",
        "publisher": "Elsevier",
        "authors": ["Ada Lovelace", "Alan Turing"],
        "author_line": "Ada Lovelace, Alan Turing",
        "published": "2026-09-12",
        "year": 2026,
        "type": "article",
        "abstract": (
            "This study investigates artificial intelligence literacy in teacher education using a mixed-methods "
            "design. We surveyed 240 pre-service teachers, conducted 18 interviews, and evaluated a six-week "
            "learning analytics intervention. The analysis shows significant improvements in AI literacy and "
            "highlights implications for curriculum design, professional development, and future research on "
            "generative AI in education. The framework offers an interdisciplinary bridge between learning "
            "sciences and teacher education."
        ),
        "cited_by_count": 12,
        "oa_url": "",
        "landing_url": "https://doi.org/10.1234/test.1",
        "topics": ["Artificial Intelligence in Education", "Teacher Education", "Learning Analytics"],
        "domains": ["Social Sciences"],
        "source": "openalex",
        "venue_type": "journal",
        "source_id": "S123",
        "source_count": 2,
        "query_fields": ["f_ai"],
        "venue_citedness": 4.2,
        "venue_works": 5000,
    }
    candidate.update(overrides)
    return candidate


def sample_profile():
    profile = default_profile()
    profile["fields"] = [{
        "id": "f_ai",
        "name": "人工智能教育",
        "keywords": ["artificial intelligence in education", "AI literacy", "learning analytics"],
        "color": "#2F5D50",
        "weight": 1,
    }]
    return profile


class RankingTests(unittest.TestCase):
    def test_scoring_with_domains_does_not_crash(self):
        # 回归测试：domains 曾经被误传为 int，导致 150/190 个候选被静默丢弃
        scored = score_candidate(
            sample_candidate(), sample_profile()["fields"], sample_profile(), None,
            date(2026, 10, 26), {"liked": [], "disliked": []},
        )
        self.assertGreater(scored["rank_score"], 0)
        self.assertIn("signals", scored)
        self.assertIn("Social Sciences", scored["signals"]["inspiration"])
        self.assertEqual(set(scored["dimension_scores"]), set(DEFAULT_WEIGHTS))

    def test_venue_metric_affects_reliability(self):
        good = score_candidate(sample_candidate(), sample_profile()["fields"], sample_profile(), None,
                               date(2026, 10, 26), {"liked": [], "disliked": []})
        weak = score_candidate(sample_candidate(venue_citedness=0.0, venue_works=200, cited_by_count=0,
                                                source_count=1),
                               sample_profile()["fields"], sample_profile(), None,
                               date(2026, 10, 26), {"liked": [], "disliked": []})
        self.assertGreater(good["dimension_scores"]["reliability"],
                           weak["dimension_scores"]["reliability"])

    def test_select_picks_diversifies_fields(self):
        ai = score_candidate(sample_candidate(), sample_profile()["fields"], sample_profile(), None,
                             date(2026, 10, 26), {"liked": [], "disliked": []})
        ai2 = dict(ai)
        ai2["id"] = "doi:10.1234/test.2"
        ai2["doi"] = "10.1234/test.2"
        ai2["title"] = "A second AI education paper"
        ai2["rank_score"] = ai["rank_score"] - 1
        other = score_candidate(
            sample_candidate(
                id="doi:10.1234/test.3", doi="10.1234/test.3",
                title="Research evaluation and open science: a bibliometric analysis",
                journal="Scientometrics",
                topics=["Research Evaluation", "Open Science"],
                query_fields=["f_science"],
            ),
            [{**sample_profile()["fields"][0], "id": "f_science", "name": "科学学",
              "keywords": ["research evaluation", "open science"]}],
            sample_profile(), None, date(2026, 10, 26), {"liked": [], "disliked": []},
        )
        other["fields"] = [{"id": "f_science", "name": "科学学", "color": "#B24E36",
                            "weight": 1, "score": 70, "matched": ["open science"]}]
        other["rank_score"] = ai["rank_score"] - 0.5
        picks = select_picks([ai, ai2, other], 2)
        self.assertEqual(len(picks), 2)
        self.assertIn("f_science", {f["id"] for p in picks for f in p.get("fields", [])})

    def test_decoration_totals(self):
        day = {"date": "2026-10-26", "picks": [{
            "id": "doi:10.1/x", "doi": "10.1/x", "scores": {k: 4 for k in DEFAULT_WEIGHTS},
        }]}
        decorated = decorate_day(day, sample_profile(), {"2026-10-26": day})
        self.assertEqual(decorated["picks"][0]["total"], 4.0)
        self.assertEqual(decorated["picks"][0]["total_pct"], 80)
        self.assertEqual(decorated["scored_count"], 1)

    def test_total_of(self):
        self.assertEqual(total_of({"scores": {"relevance": 5, "novelty": 3}}), 4.0)
        self.assertIsNone(total_of({"scores": {}}))

    def test_venue_gate_filters_low_metric(self):
        profile = sample_profile()
        profile["min_venue_citedness"] = 2.0
        profile["venue_missing_policy"] = "exclude"
        profile["daily_count"] = 1
        good = sample_candidate()
        weak = sample_candidate(
            id="doi:10.1234/weak", doi="10.1234/weak",
            title="A low metric journal paper about artificial intelligence education",
            venue_citedness=0.2, venue_works=3000,
        )
        fetched = {"candidates": [good, weak], "status": [], "errors": [], "raw_count": 2}
        day = build_day(fetched, profile, "2026-10-26", None, None, {"liked": [], "disliked": []})
        self.assertEqual(day["meta"]["candidate_count"], 1)
        self.assertEqual(day["meta"]["venue_filtered"], 1)
        self.assertEqual(day["picks"][0]["doi"], "10.1234/test.1")

    def test_prior_exclusions_read_forever_unread_window(self):
        scored = sample_candidate(doi="10.1234/scored", id="doi:10.1234/scored")
        scored["scores"] = {k: 4 for k in DEFAULT_WEIGHTS}
        old_unread = sample_candidate(doi="10.1234/old", id="doi:10.1234/old",
                                      title="Old unread paper about artificial intelligence education")
        recent_unread = sample_candidate(doi="10.1234/recent", id="doi:10.1234/recent",
                                         title="Recent unread paper about artificial intelligence education")
        state = {"days": {
            "2026-01-01": {"picks": [scored]},
            "2026-02-01": {"picks": [old_unread]},
            "2026-09-20": {"picks": [recent_unread]},
        }}
        excluded = prior_exclusions(state, "2026-10-26", days=45)
        self.assertIn("10.1234/scored", excluded)
        self.assertIn("title:" + title_key(scored["title"]), excluded)
        self.assertNotIn("10.1234/old", excluded)
        self.assertIn("10.1234/recent", excluded)

    def test_venue_gate_missing_policy(self):
        profile = sample_profile()
        profile["min_venue_citedness"] = 2.0
        profile["venue_missing_policy"] = "include"
        profile["daily_count"] = 1
        missing = sample_candidate(
            id="doi:10.1234/missing", doi="10.1234/missing",
            title="Artificial intelligence education without a venue metric",
            venue_citedness=None,
        )
        fetched = {"candidates": [missing], "status": [], "errors": [], "raw_count": 1}
        day = build_day(fetched, profile, "2026-10-26", None, None, {"liked": [], "disliked": []})
        self.assertEqual(len(day["picks"]), 1)
        self.assertEqual(day["meta"]["venue_filtered"], 0)


class PreferenceTests(unittest.TestCase):
    def test_focus_increases_weight(self):
        profile = default_profile()
        _, base = effective_weights(profile, None)
        _, focused = effective_weights(profile, {"focus": ["novelty", "inspiration"]})
        self.assertGreater(focused["novelty"], base["novelty"])
        self.assertGreater(focused["inspiration"], base["inspiration"])
        self.assertAlmostEqual(sum(focused.values()), 100, delta=0.5)

    def test_validate_profile_preserves_key(self):
        previous = default_profile()
        previous["openalex_api_key"] = "secret-key"
        payload = {"display_name": "Tester", "fields": [
            {"name": "AI", "keywords": "AI literacy, learning analytics"}
        ], "sources": ["openalex"]}
        updated = validate_profile(payload, previous)
        self.assertEqual(updated["openalex_api_key"], "secret-key")
        cleared = validate_profile({**payload, "clear_openalex_key": True}, previous)
        self.assertEqual(cleared["openalex_api_key"], "")

    def test_public_profile_masks_key(self):
        profile = default_profile()
        profile["openalex_api_key"] = "a"
        public = public_profile(profile)
        self.assertTrue(public["has_openalex_key"])
        self.assertNotIn("openalex_api_key", public)

    def test_default_sources(self):
        sources = default_profile()["sources"]
        self.assertIn("openalex", sources)
        self.assertTrue(set(sources).issubset(set(SOURCE_KEYS)))

    def test_daily_count_range(self):
        payload = {"display_name": "T", "fields": [
            {"name": "AI", "keywords": "AI literacy"}
        ], "sources": ["openalex"]}
        for value, expected in [(1, 1), (3, 3), (5, 5), (9, 5), (0, 1)]:
            profile = validate_profile({**payload, "daily_count": value}, default_profile())
            self.assertEqual(profile["daily_count"], expected)

    def test_institution_url(self):
        url = institution_url("https://login.ezproxy.edu/login?url={url}", "10.1000/xyz")
        self.assertIn("https%3A%2F%2Fdoi.org%2F10.1000%2Fxyz", url)
        self.assertIn("10.1000/xyz", institution_url("https://x.edu/?doi={doi}", "10.1000/xyz"))


class SourceTests(unittest.TestCase):
    def test_doi_normalization(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1000/ABC"), "10.1000/abc")
        self.assertEqual(normalize_doi("doi: 10.1000/xyz."), "10.1000/xyz")
        self.assertEqual(normalize_doi("not-a-doi"), "")

    def test_smart_title(self):
        self.assertEqual(
            _smart_title("BRIDGING OR WIDENING THE GAP? A REVIEW OF AI IN EDUCATION"),
            "Bridging or Widening the Gap? A Review of AI in Education",
        )
        self.assertEqual(_smart_title("Mixed Case Title Stays"), "Mixed Case Title Stays")

    def test_merge_duplicate_sources(self):
        a = sample_candidate(source="openalex", abstract="short", source_count=1)
        b = sample_candidate(source="crossref", abstract="a much longer abstract from crossref",
                             source_count=1, cited_by_count=30)
        merged = _merge_candidates([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_count"], 2)
        self.assertGreater(len(merged[0]["abstract"]), len(a["abstract"]))
        self.assertEqual(merged[0]["cited_by_count"], 30)

    def test_merge_preprint_and_journal_by_title(self):
        preprint = sample_candidate(
            id="doi:10.48550/arxiv.123", doi="10.48550/arxiv.123",
            journal="arXiv · cs.CL", type="preprint", venue_type="repository",
            source="arxiv",
        )
        published = sample_candidate(
            id="doi:10.1234/journal", doi="10.1234/journal",
            journal="Computers & Education", type="article", venue_type="journal",
            source="crossref",
        )
        merged = _merge_candidates([preprint, published])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["doi"], "10.1234/journal")
        self.assertEqual(merged[0]["journal"], "Computers & Education")

class BackupTests(unittest.TestCase):
    def test_backup_strips_api_keys(self):
        state = {"profile": default_profile(), "days": {}}
        state["profile"]["openalex_api_key"] = "secret"
        backup = build_backup(state)
        self.assertNotIn("secret", backup)
        self.assertNotIn("openalex_api_key", backup)


class ReportTests(unittest.TestCase):
    def test_report_escapes_html(self):
        day = {
            "date": "2026-10-26", "issue": 1, "generated_at": "2026-10-26T12:00:00+08:00",
            "picks": [{
                "id": "doi:10.1/x", "doi": "10.1/x", "title": "<script>alert(1)</script>",
                "authors": [], "author_line": "A", "journal": "J", "published": "2026-01-01",
                "abstract": "Abstract", "scores": {}, "signals": {},
            }],
            "meta": {"sources": [], "candidate_count": 1},
        }
        html_text = build_report_html(day, sample_profile())
        self.assertNotIn("<script>alert(1)</script>", html_text)
        self.assertIn("&lt;script&gt;", html_text)
        self.assertIn("scholar.google.com", html_text)

    def test_csv_has_bom_and_headers(self):
        state = {"days": {}}
        csv_text = build_csv(state)
        self.assertTrue(csv_text.startswith("\ufeff"))
        self.assertIn("相关性", csv_text.splitlines()[0])


class StoreTests(unittest.TestCase):
    def test_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = Store(path)
            store.state["profile"]["display_name"] = "Tester"
            store.save()
            self.assertTrue(path.exists())
            reloaded = Store(path).snapshot()
            self.assertEqual(reloaded["profile"]["display_name"], "Tester")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["profile"]["display_name"], "Tester")

    def test_auto_backup_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = Store(path)
            store.state["profile"]["display_name"] = "第一版"
            store.save()
            store.state["profile"]["display_name"] = "第二版"
            store.save()
            backups = store.list_backups()
            self.assertGreaterEqual(len(backups), 1)
            self.assertTrue(all(name["size"] > 0 for name in backups))
            restored = store.restore_backup(backups[-1]["name"])
            self.assertEqual(restored["profile"]["display_name"], "第一版")
            # 恢复前也会把当前版本留一份
            self.assertGreaterEqual(len(store.list_backups()), 2)

    def test_backup_is_created_on_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = Store(path)
            store.state["profile"]["display_name"] = "启动版本"
            store.save()
            Store(path)
            self.assertGreaterEqual(len(Store(path).list_backups()), 1)

    def test_missing_state_recovers_latest_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = Store(path)
            store.state["profile"]["display_name"] = "第一版"
            store.save()
            store.state["profile"]["display_name"] = "第二版"
            store.save()
            path.unlink()
            recovered = Store(path).snapshot()
            self.assertEqual(recovered["profile"]["display_name"], "第一版")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
