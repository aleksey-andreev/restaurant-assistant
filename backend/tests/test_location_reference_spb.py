"""Location reference search for spb and msk."""

from __future__ import annotations

import unittest

from app.services.graph_runner import validate_recommendation_requirements_fields
from app.services.location_reference import (
    apply_canonical_location_to_req,
    build_collect_requirements_location_hint,
    location_is_canonical,
    location_reference_enabled,
    search_districts,
    search_metro,
    validate_recommendation_requirements_fields_with_location,
)

# Minimal SPB district subset (matches init_db seed labels).
SPB_DISTRICTS_FIXTURE = [
    {"district_label": "Центральный район", "district_norm": "центральный район"},
    {"district_label": "Адмиралтейский район", "district_norm": "адмиралтейский район"},
    {"district_label": "Невский район", "district_norm": "невский район"},
]

MSK_DISTRICTS_FIXTURE = [
    {"district_label": "Тверской район", "district_norm": "тверской район"},
    {"district_label": "Хамовники", "district_norm": "хамовники"},
    {"district_label": "Арбат", "district_norm": "арбат"},
]


class TestLocationReferenceSpb(unittest.TestCase):
    def test_search_district_centre(self) -> None:
        hits = search_districts(SPB_DISTRICTS_FIXTURE, "центр", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["district_label"], "Центральный район")
        self.assertGreaterEqual(hits[0]["score"], 0.7)

    def test_apply_canonical_centre(self) -> None:
        req = {
            "city_slug": "spb",
            "intent": "search",
            "location": {"type": "area", "value": "центр"},
        }
        out, meta = apply_canonical_location_to_req(
            req,
            districts=SPB_DISTRICTS_FIXTURE,
            metro_names=["Невский проспект"],
        )
        self.assertEqual(out["location"]["value"], "Центральный район")
        self.assertIn("location_auto", meta)

    def test_search_metro_prefix(self) -> None:
        metros = ["Невский проспект", "Гостиный двор", "Площадь Восстания"]
        hits = search_metro(metros, "невский", limit=5)
        self.assertTrue(hits)
        self.assertIn("Невский", hits[0]["metro_name"])

    def test_validate_rejects_non_canonical_area(self) -> None:
        req = {
            "city_slug": "spb",
            "city": "Санкт-Петербург",
            "intent": "search",
            "location": {"type": "area", "value": "центр"},
        }
        miss = validate_recommendation_requirements_fields_with_location(
            req,
            districts=SPB_DISTRICTS_FIXTURE,
            metro_names=[],
            base_validate=validate_recommendation_requirements_fields,
        )
        self.assertIn("location_or_cuisine", miss)

    def test_validate_accepts_canonical_area(self) -> None:
        req = {
            "city_slug": "spb",
            "city": "Санкт-Петербург",
            "intent": "search",
            "location": {"type": "area", "value": "Центральный район"},
        }
        miss = validate_recommendation_requirements_fields_with_location(
            req,
            districts=SPB_DISTRICTS_FIXTURE,
            metro_names=[],
            base_validate=validate_recommendation_requirements_fields,
        )
        self.assertNotIn("location_or_cuisine", miss)

    def test_location_is_canonical(self) -> None:
        loc = {"type": "area", "value": "Центральный район"}
        self.assertTrue(
            location_is_canonical(
                loc,
                city_slug="spb",
                districts=SPB_DISTRICTS_FIXTURE,
                metro_names=[],
            )
        )


class TestLocationReferenceMsk(unittest.TestCase):
    def test_location_reference_enabled(self) -> None:
        self.assertTrue(location_reference_enabled("msk"))
        self.assertTrue(location_reference_enabled("spb"))
        self.assertFalse(location_reference_enabled("voronezh"))

    def test_collect_hint_for_msk(self) -> None:
        hint = build_collect_requirements_location_hint("msk")
        self.assertIn("Москва", hint)
        self.assertIn("search_districts", hint)

    def test_search_district_tverskoy(self) -> None:
        hits = search_districts(MSK_DISTRICTS_FIXTURE, "тверской", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["district_label"], "Тверской район")

    def test_search_district_khamovniki(self) -> None:
        hits = search_districts(MSK_DISTRICTS_FIXTURE, "хамовники", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["district_label"], "Хамовники")

    def test_apply_canonical_gorny_universitet_spb(self) -> None:
        """User wording «Горный университет» → canonical «Горный институт»."""
        req = {
            "city_slug": "spb",
            "intent": "search",
            "location": {"type": "metro", "value": "Горный университет"},
        }
        metros = ["Горный институт", "Невский проспект"]
        out, meta = apply_canonical_location_to_req(
            req,
            districts=SPB_DISTRICTS_FIXTURE,
            metro_names=metros,
        )
        self.assertEqual(out["location"]["value"], "Горный институт")
        self.assertIn("location_auto", meta)
        miss = validate_recommendation_requirements_fields_with_location(
            out,
            districts=SPB_DISTRICTS_FIXTURE,
            metro_names=metros,
            base_validate=validate_recommendation_requirements_fields,
        )
        self.assertNotIn("location_or_cuisine", miss)

    def test_apply_canonical_metro(self) -> None:
        req = {
            "city_slug": "msk",
            "intent": "search",
            "location": {"type": "metro", "value": "тверская"},
        }
        out, meta = apply_canonical_location_to_req(
            req,
            districts=MSK_DISTRICTS_FIXTURE,
            metro_names=["Тверская", "Киевская", "Сокол"],
        )
        self.assertEqual(out["location"]["value"], "Тверская")
        self.assertIn("location_auto", meta)

    def test_validate_accepts_canonical_msk_area(self) -> None:
        req = {
            "city_slug": "msk",
            "city": "Москва",
            "intent": "search",
            "location": {"type": "area", "value": "Хамовники"},
        }
        miss = validate_recommendation_requirements_fields_with_location(
            req,
            districts=MSK_DISTRICTS_FIXTURE,
            metro_names=["Тверская"],
            base_validate=validate_recommendation_requirements_fields,
        )
        self.assertNotIn("location_or_cuisine", miss)


if __name__ == "__main__":
    unittest.main()
