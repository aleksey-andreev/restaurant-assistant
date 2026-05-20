from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.services.toka_specific_restaurant_resolver import resolve_specific_restaurant_candidates


class SpecificRestaurantResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_when_single_strong_db_match(self) -> None:
        db_candidates = [
            {
                "name": "White Rabbit",
                "url": "https://www.afisha.ru/msk/restaurant/white-rabbit/",
                "address": "Смоленская площадь, 3",
            },
            {"name": "Sage", "url": "https://www.afisha.ru/msk/restaurant/sage/", "address": "1-я Тверская"},
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.get_session_maker",
                return_value=lambda: None,
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.AfishaCatalogRepository"
                ".find_rows_for_city_by_restaurant_name",
                return_value=(db_candidates, "ilike_phrase"),
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="msk",
                restaurant_name="White Rabbit",
            )

        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["selected"].get("name"), "White Rabbit")
        self.assertEqual(out["db_match_count"], 2)
        self.assertEqual(out["match_mode"], "ilike_phrase")

    async def test_ambiguous_when_multiple_name_matches(self) -> None:
        db_candidates = [
            {"name": "Сыроварня на Красном Октябре", "url": "u1", "address": "Берсеневская наб."},
            {"name": "Сыроварня на Усачевском", "url": "u2", "address": "ул. Усачева"},
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.get_session_maker",
                return_value=lambda: None,
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.AfishaCatalogRepository"
                ".find_rows_for_city_by_restaurant_name",
                return_value=(db_candidates, "ilike_tokens"),
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="msk",
                restaurant_name="Сыроварня",
            )

        self.assertEqual(out["status"], "ambiguous")
        self.assertIsNone(out.get("selected"))
        self.assertGreaterEqual(len(out.get("candidates") or []), 2)

    async def test_not_found_when_catalog_empty(self) -> None:
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.get_session_maker",
                return_value=lambda: None,
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.AfishaCatalogRepository"
                ".find_rows_for_city_by_restaurant_name",
                return_value=([], "ilike_phrase"),
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="msk",
                restaurant_name="Unknown Place",
            )

        self.assertEqual(out["status"], "not_found")
        self.assertEqual(out.get("candidates"), [])
        self.assertEqual(out["db_match_count"], 0)

    async def test_skips_candidates_without_address(self) -> None:
        db_candidates = [
            {
                "name": "Империя",
                "url": "u1",
                "address": "",
            },
            {
                "name": "Империя",
                "url": "u3",
                "address": "Тверская, 7",
            },
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.get_session_maker",
                return_value=lambda: None,
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.AfishaCatalogRepository"
                ".find_rows_for_city_by_restaurant_name",
                return_value=(db_candidates, "ilike_phrase"),
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="msk",
                restaurant_name="Империя",
            )

        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["selected"].get("url"), "u3")


if __name__ == "__main__":
    unittest.main()
