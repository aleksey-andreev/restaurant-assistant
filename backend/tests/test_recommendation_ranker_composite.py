"""Tests for cuisine prefilter and composite ranking (geo + external rating)."""

from __future__ import annotations

import unittest

from app.services.recommendation_ranker import (
    prefilter_candidates_by_cuisine,
    rank_candidates,
)


class PrefilterCuisineTest(unittest.TestCase):
    def test_keeps_all_when_no_cuisine_prefs(self) -> None:
        cands = [{"tags": ["Суши"], "url": "u1"}]
        out = prefilter_candidates_by_cuisine(cands, {})
        self.assertEqual(len(out), 1)

    def test_drops_when_wanted_mismatch(self) -> None:
        cands = [{"tags": ["Суши"], "url": "u1"}]
        out = prefilter_candidates_by_cuisine(
            cands,
            {"cuisine_wanted": ["итальянская"]},
        )
        self.assertEqual(len(out), 0)


class CompositeRankTest(unittest.TestCase):
    def test_geo_boosts_match_over_no_rating(self) -> None:
        req = {
            "location": {"type": "metro", "value": "Киевская"},
            "budget_range": {"min": 2000, "max": 5000},
            "cuisine_wanted": [],
        }
        high_geo = {
            "tags": [],
            "avg_check": {"min": 2500, "max": 3500},
            "geo_location_score": 1.0,
            "external_rating": None,
            "url": "a",
        }
        low_geo = {
            "tags": [],
            "avg_check": {"min": 2500, "max": 3500},
            "geo_location_score": 0.45,
            "external_rating": None,
            "url": "b",
        }
        r = rank_candidates([low_geo, high_geo], req)
        sc = r["scored_candidates"]
        by_url = {x["url"]: float(x["formal_score"]) for x in sc}
        self.assertGreater(by_url["a"], by_url["b"])

    def test_external_rating_when_confident(self) -> None:
        req = {"budget_range": {"min": 2000, "max": 5000}, "cuisine_wanted": []}
        with_rating = {
            "tags": [],
            "avg_check": {"min": 2500, "max": 3500},
            "external_rating": 5.0,
            "external_rating_confidence": 0.9,
            "url": "x",
        }
        no_rating = {
            "tags": [],
            "avg_check": {"min": 2500, "max": 3500},
            "external_rating": None,
            "external_rating_confidence": 0.0,
            "url": "y",
        }
        r = rank_candidates([no_rating, with_rating], req)
        sx = next(x for x in r["scored_candidates"] if x["url"] == "x")
        sy = next(x for x in r["scored_candidates"] if x["url"] == "y")
        self.assertIsNotNone(sx.get("external_rating_score"))
        self.assertIsNone(sy.get("external_rating_score"))


if __name__ == "__main__":
    unittest.main()
