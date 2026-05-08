"""Unit tests for Yandex SERP rating extraction."""

from __future__ import annotations

import unittest

from app.services.external_rating import (
    build_yandex_rating_query,
    catalog_aggregate_rating_score,
    extract_rating_from_yandex_serp_xml,
    rating_score_normalized,
    stored_yandex_rating_from_catalog,
)


class ExternalRatingExtractTest(unittest.TestCase):
    def test_extract_rating_with_maps_and_name(self) -> None:
        raw = """<?xml version="1.0"?><yandexsearch><results><group><doc>
<title>Ресторан Test, Москва</title>
<headline>рейтинг: 4,7 из 5</headline>
<url>https://yandex.ru/maps/org/test/123/</url>
</doc></group></results></yandexsearch>"""
        val, conf = extract_rating_from_yandex_serp_xml(raw, restaurant_name="Test")
        self.assertAlmostEqual(val, 4.7, places=5)
        self.assertGreaterEqual(conf, 0.45)

    def test_extract_none_without_pattern(self) -> None:
        raw = "<xml><doc><title>foo</title></doc></xml>"
        val, conf = extract_rating_from_yandex_serp_xml(raw, restaurant_name="bar")
        self.assertIsNone(val)
        self.assertLess(conf, 0.45)

    def test_rating_score_normalized(self) -> None:
        self.assertIsNone(rating_score_normalized(4.5, 0.1))
        self.assertAlmostEqual(rating_score_normalized(5.0, 0.9), 1.0)

    def test_catalog_aggregate_rating_score(self) -> None:
        ce = {"from_ld": {"aggregate_rating": {"rating_value": 4.5, "review_count": 12}}}
        r, conf = catalog_aggregate_rating_score(ce)
        self.assertAlmostEqual(r, 4.5, places=3)
        self.assertGreaterEqual(conf, 0.45)

    def test_catalog_aggregate_rating_missing(self) -> None:
        self.assertEqual(catalog_aggregate_rating_score(None), (None, 0.0))
        self.assertEqual(catalog_aggregate_rating_score({}), (None, 0.0))

    def test_stored_yandex_rating_from_catalog(self) -> None:
        r, c = stored_yandex_rating_from_catalog({"yandex_rating": 4.2, "yandex_rating_confidence": 0.8})
        self.assertAlmostEqual(r, 4.2, places=3)
        self.assertGreaterEqual(c, 0.45)

    def test_stored_yandex_rating_default_confidence(self) -> None:
        r, c = stored_yandex_rating_from_catalog({"yandex_rating": 5.0})
        self.assertAlmostEqual(r, 5.0, places=3)
        self.assertGreaterEqual(c, 0.45)

    def test_build_yandex_rating_query_includes_address(self) -> None:
        q = build_yandex_rating_query(
            "Теремок",
            "Москва",
            address="ул. Тверская, 10",
            max_query_len=500,
        )
        self.assertIn("Теремок", q)
        self.assertIn("Тверская", q)
        self.assertIn("Москва", q)
        self.assertTrue(q.startswith("рейтинг ресторана"))

    def test_build_yandex_rating_query_truncates(self) -> None:
        long_addr = "ул. " + "а" * 300
        q = build_yandex_rating_query("X", "СПб", address=long_addr, max_query_len=80)
        self.assertLessEqual(len(q), 80)


if __name__ == "__main__":
    unittest.main()
