"""Lightweight regression checks for recommendation graph wiring."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.graph_runner import format_search_plan_summary


class GraphPipelineContractTest(unittest.TestCase):
    def test_yandex_afisha_queries_do_not_use_loc_part(self) -> None:
        root = Path(__file__).resolve().parents[1]
        src = (root / "app" / "services" / "graph_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("loc_part", src, "metro/area must not be injected into Afisha SERP queries")

    def test_search_plan_summary_location_labels_russian(self) -> None:
        base = {
            "city": "Москва",
            "party_size": 2,
            "budget_range": {"min": 1000, "max": 2000},
            "cuisine_wanted": [],
            "cuisine_avoid": [],
        }
        metro = format_search_plan_summary(
            {**base, "location": {"type": "metro", "value": "Киевская"}}
        )
        self.assertIn("- Локация: метро Киевская", metro)
        self.assertNotIn("metro:", metro)

        area = format_search_plan_summary(
            {**base, "location": {"type": "area", "value": "Хамовники"}}
        )
        self.assertIn("- Локация: район Хамовники", area)
        self.assertNotIn("area:", area)


if __name__ == "__main__":
    unittest.main()
