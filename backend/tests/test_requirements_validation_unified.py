"""Unified requirements validation (compute_global_requirements_missing)."""

from __future__ import annotations

import unittest

from app.services.graph_runner import compute_global_requirements_missing
from app.services.location_reference import apply_canonical_location_to_req

SPB_DISTRICTS = [
    {"district_label": "Центральный район", "district_norm": "центральный район"},
]


class TestRequirementsValidationUnified(unittest.TestCase):
    def test_named_restaurant_only_city_and_name(self) -> None:
        miss = compute_global_requirements_missing(
            {
                "intent": "named_restaurant",
                "city": "Москва",
                "city_slug": "msk",
                "restaurant_name": "Эвкалипт",
            }
        )
        self.assertEqual(miss, [])

    def test_named_restaurant_missing_name(self) -> None:
        miss = compute_global_requirements_missing(
            {"intent": "named_restaurant", "city": "Москва", "city_slug": "msk"}
        )
        self.assertEqual(miss, ["restaurant_name"])

    def test_search_non_canonical_metro_without_ref(self) -> None:
        """Without districts seed, raw metro string satisfies base validator."""
        miss = compute_global_requirements_missing(
            {
                "intent": "search",
                "city": "Санкт-Петербург",
                "city_slug": "spb",
                "location": {"type": "metro", "value": "Горный университет"},
            }
        )
        self.assertEqual(miss, [])

    def test_search_synonym_metro_passes_with_fuzzy_ref(self) -> None:
        """Fuzzy metro match (п.2) — валидация проходит без ручных алиасов."""
        metros = ["Горный институт", "Невский проспект"]
        req = {
            "intent": "search",
            "city": "Санкт-Петербург",
            "city_slug": "spb",
            "location": {"type": "metro", "value": "Горный университет"},
        }
        miss = compute_global_requirements_missing(
            req,
            districts=SPB_DISTRICTS,
            metro_names=metros,
        )
        self.assertEqual(miss, [])

    def test_apply_canonical_rewrites_metro_label(self) -> None:
        metros = ["Горный институт", "Невский проспект"]
        req = {
            "intent": "search",
            "city": "Санкт-Петербург",
            "city_slug": "spb",
            "location": {"type": "metro", "value": "Горный университет"},
        }
        canonical, meta = apply_canonical_location_to_req(
            req, districts=SPB_DISTRICTS, metro_names=metros
        )
        self.assertEqual(canonical["location"]["value"], "Горный институт")
        self.assertIn("location_auto", meta)


if __name__ == "__main__":
    unittest.main()
