"""Unit tests for LLM geo match helpers."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.services.llm_geo_match import (
    build_restaurant_address_for_geo,
    fingerprint_user_location,
    llm_geo_infer_one,
    location_score_from_result,
    match_inferred_to_user,
    parse_llm_geo_inference_json,
    parse_llm_geo_json,
)


class LlmGeoMatchHelpersTest(unittest.TestCase):
    def test_parse_inference_valid_json(self) -> None:
        raw = '{"inferred_primary_metro": "Смоленская", "inferred_district_or_area": "ЦАО"}'
        im, ia, ok = parse_llm_geo_inference_json(raw)
        self.assertTrue(ok)
        self.assertEqual(im, "Смоленская")
        self.assertEqual(ia, "ЦАО")

    def test_parse_llm_geo_json_legacy_wrapper(self) -> None:
        raw = '{"inferred_primary_metro": "Смоленская", "inferred_district_or_area": "ЦАО"}'
        r, m, a = parse_llm_geo_json(raw)
        self.assertEqual(r, "uncertain")
        self.assertEqual(m, "Смоленская")
        self.assertEqual(a, "ЦАО")

    def test_parse_inference_invalid(self) -> None:
        im, ia, ok = parse_llm_geo_inference_json("not json")
        self.assertFalse(ok)
        self.assertIsNone(im)
        self.assertIsNone(ia)

    def test_parse_inference_strips_fences(self) -> None:
        raw = (
            "```json\n"
            '{"inferred_primary_metro": null, "inferred_district_or_area": null}\n'
            "```"
        )
        im, ia, ok = parse_llm_geo_inference_json(raw)
        self.assertTrue(ok)
        self.assertIsNone(im)
        self.assertIsNone(ia)

    def test_location_score_from_result(self) -> None:
        self.assertEqual(location_score_from_result("match"), 1.0)
        self.assertEqual(location_score_from_result("uncertain"), 0.45)
        self.assertEqual(location_score_from_result("no_match"), 0.0)

    def test_fingerprint_user_location(self) -> None:
        self.assertEqual(
            fingerprint_user_location({"type": "metro", "value": " Киевская "}),
            "metro:киевская",
        )
        self.assertEqual(fingerprint_user_location({"type": "none", "value": None}), "")

    def test_build_restaurant_address_for_geo(self) -> None:
        s = build_restaurant_address_for_geo(
            {
                "address": "Москва, ул. Тестовая, 1",
                "metro": "Киевская",
                "name": "Ресторан X",
            }
        )
        self.assertIn("Москва", s)
        self.assertNotIn("метро", s.lower())

    def test_match_metro_overlap(self) -> None:
        self.assertEqual(
            match_inferred_to_user("Арбатская", None, {"type": "metro", "value": "Арбатская"}),
            "match",
        )
        self.assertEqual(
            match_inferred_to_user("м. Арбатская", None, {"type": "metro", "value": "Арбатская"}),
            "match",
        )

    def test_match_metro_no_match(self) -> None:
        self.assertEqual(
            match_inferred_to_user("Киевская", None, {"type": "metro", "value": "Арбатская"}),
            "no_match",
        )

    def test_match_metro_via_extra_osm_stations(self) -> None:
        """User station can match any of extra_metro_names (e.g. OSM nearest list)."""
        self.assertEqual(
            match_inferred_to_user(
                "Киевская",
                None,
                {"type": "metro", "value": "Достоевская"},
                extra_metro_names=["Владимирская", "Достоевская"],
            ),
            "match",
        )

    def test_match_gorny_institute_inferred_only(self) -> None:
        """Catalog often has geo_inferred_metro but empty geo_osm_metros (SPB)."""
        self.assertEqual(
            match_inferred_to_user(
                "Горный институт",
                None,
                {"type": "metro", "value": "Горный институт"},
            ),
            "match",
        )
        self.assertEqual(
            match_inferred_to_user(
                "Маяковская",
                None,
                {"type": "metro", "value": "Горный институт"},
            ),
            "no_match",
        )

    def test_match_area(self) -> None:
        self.assertEqual(
            match_inferred_to_user(None, "Василеостровский", {"type": "area", "value": "Василеостровский"}),
            "match",
        )

    def test_match_empty_inference(self) -> None:
        self.assertEqual(
            match_inferred_to_user(None, None, {"type": "metro", "value": "Арбатская"}),
            "uncertain",
        )

    def test_match_no_user_constraint(self) -> None:
        self.assertEqual(match_inferred_to_user("X", "Y", {"type": "none", "value": ""}), "match")


class LlmGeoMatchAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_llm_geo_infer_uses_cache(self) -> None:
        cache: dict = {}
        chat = AsyncMock(
            return_value='{"inferred_primary_metro": "Арбатская", "inferred_district_or_area": null}'
        )
        params = {"model": "dummy"}
        im1, ia1 = await llm_geo_infer_one(
            chat,
            city="Москва",
            restaurant_address="Москва, центр",
            restaurant_name="Кафе",
            node_params=params,
            cache=cache,
            isolation_key="https://www.afisha.ru/msk/restaurant/foo/",
        )
        im2, ia2 = await llm_geo_infer_one(
            chat,
            city="Москва",
            restaurant_address="Москва, центр",
            restaurant_name="Кафе",
            node_params=params,
            cache=cache,
            isolation_key="https://www.afisha.ru/msk/restaurant/foo/",
        )
        self.assertEqual(im1, "Арбатская")
        self.assertEqual(im2, "Арбатская")
        self.assertIsNone(ia1)
        self.assertEqual(chat.await_count, 1)

    async def test_different_isolation_keys_two_llm_calls(self) -> None:
        cache: dict = {}
        chat = AsyncMock(
            return_value='{"inferred_primary_metro": null, "inferred_district_or_area": null}'
        )
        params = {"model": "dummy"}
        await llm_geo_infer_one(
            chat,
            city="Москва",
            restaurant_address="одинаковый текст адреса",
            restaurant_name="А",
            node_params=params,
            cache=cache,
            isolation_key="url-a",
        )
        await llm_geo_infer_one(
            chat,
            city="Москва",
            restaurant_address="одинаковый текст адреса",
            restaurant_name="Б",
            node_params=params,
            cache=cache,
            isolation_key="url-b",
        )
        self.assertEqual(chat.await_count, 2)


if __name__ == "__main__":
    unittest.main()
