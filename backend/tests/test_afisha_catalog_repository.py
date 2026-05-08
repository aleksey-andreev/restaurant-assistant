"""Unit tests for Afisha catalog repository helpers."""

from __future__ import annotations

import unittest

from app.storage.afisha_catalog_repository import (
    _catalog_row_has_prefetch,
    candidate_dict_to_enriched_row,
    catalog_entry_to_candidate,
)


class AfishaCatalogRepositoryHelpersTest(unittest.TestCase):
    def test_catalog_entry_to_candidate_shape(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": "Foo",
            "address": "Санкт-Петербург, ул. Барная, 1",
            "metro": "Барная",
            "tags": ["Итальянская"],
            "avg_check": {"raw": "2000–3000 ₽", "min": 2000, "max": 3000},
            "flags": [],
            "venue_closed": False,
            "open_now": None,
        }
        c = catalog_entry_to_candidate(row)
        self.assertEqual(c["url"], row["url"])
        self.assertIsNone(c.get("metro"))
        self.assertEqual(c["tags"], ["Итальянская"])
        self.assertTrue(c["debug"].get("from_catalog"))
        self.assertNotIn("geo_inferred_metro", c)

    def test_catalog_entry_flags_dict_passthrough(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": "Foo",
            "address": "Санкт-Петербург, ул. X, 1",
            "metro": None,
            "tags": [],
            "flags": {"parking": True, "banquets": False, "delivery": None},
            "open_now": {"is_open_now": True, "raw": "Открыто до 23:00"},
            "card_extras": {"from_ld": {"telephone": "+7 800"}},
            "venue_closed": False,
        }
        c = catalog_entry_to_candidate(row)
        self.assertEqual(c.get("flags"), {"parking": True, "banquets": False, "delivery": None})
        self.assertEqual(c.get("open_now"), {"is_open_now": True, "raw": "Открыто до 23:00"})
        self.assertEqual(c.get("card_extras"), {"from_ld": {"telephone": "+7 800"}})

    def test_catalog_entry_includes_yandex_rating(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": "Foo",
            "address": "СПб, ул. X, 1",
            "metro": None,
            "tags": [],
            "flags": {},
            "venue_closed": False,
            "open_now": None,
            "yandex_rating": 4.5,
            "yandex_rating_confidence": 0.9,
        }
        c = catalog_entry_to_candidate(row)
        self.assertAlmostEqual(c.get("yandex_rating"), 4.5, places=3)
        self.assertAlmostEqual(c.get("yandex_rating_confidence"), 0.9, places=3)

    def test_catalog_entry_includes_geo_when_present(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": "Foo",
            "address": "Санкт-Петербург, ул. X, 1",
            "metro": None,
            "tags": [],
            "venue_closed": False,
            "open_now": None,
            "geo_inferred_metro": "Площадь Восстания",
            "geo_inferred_area": "Центральный",
        }
        c = catalog_entry_to_candidate(row)
        self.assertEqual(c.get("geo_inferred_metro"), "Площадь Восстания")
        self.assertEqual(c.get("geo_inferred_area"), "Центральный")

    def test_prefetch_false_with_tags_only(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": None,
            "metro": None,
            "address": None,
            "tags": ["Грузинская"],
            "venue_closed": False,
        }
        self.assertFalse(_catalog_row_has_prefetch(row))

    def test_prefetch_false_when_closed(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": "X",
            "venue_closed": True,
        }
        self.assertFalse(_catalog_row_has_prefetch(row))

    def test_candidate_dict_preserves_flags_dict(self) -> None:
        cand = {
            "url": "https://www.afisha.ru/spb/restaurant/x/",
            "name": "X",
            "flags": {"parking": True, "banquets": None},
            "open_now": {"is_open_now": None, "raw": None},
            "card_extras": {"from_ld": {"description": "Test"}},
        }
        row = candidate_dict_to_enriched_row("spb", cand)
        self.assertEqual(row["flags"], {"parking": True, "banquets": None})
        self.assertEqual(row["open_now"], {"is_open_now": None, "raw": None})
        self.assertEqual(row["card_extras"], {"from_ld": {"description": "Test"}})

    def test_prefetch_false_with_flags_only(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": None,
            "address": None,
            "tags": [],
            "flags": {"parking": True},
            "venue_closed": False,
        }
        self.assertFalse(_catalog_row_has_prefetch(row))

    def test_prefetch_true_with_minimum_required_fields(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": "Foo",
            "address": "СПб, ул. X, 1",
            "geo_inferred_area": "Центральный район",
            "venue_closed": False,
        }
        self.assertTrue(_catalog_row_has_prefetch(row))

    def test_prefetch_false_metro_only(self) -> None:
        row = {
            "url": "https://www.afisha.ru/spb/restaurant/foo/",
            "name": None,
            "metro": "Невский проспект",
            "address": None,
            "tags": [],
            "venue_closed": False,
        }
        self.assertFalse(_catalog_row_has_prefetch(row))


if __name__ == "__main__":
    unittest.main()
