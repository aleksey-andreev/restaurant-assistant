"""Metro catalog fetch and normalization."""

from __future__ import annotations

import unittest

from app.services.metro_catalog_fetch import (
    fetch_msk_districts_from_wikipedia,
    fetch_msk_metro_stations_from_wikipedia,
    fetch_spb_metro_stations_from_wikipedia,
    wikipedia_title_to_msk_district_label,
    wikipedia_title_to_station_label,
)
from app.storage.afisha_catalog_repository import norm_metro_station_key


class TestMetroCatalogFetch(unittest.TestCase):
    def test_wikipedia_title_cleanup(self) -> None:
        self.assertEqual(
            wikipedia_title_to_station_label("Гостиный двор (станция метро)"),
            "Гостиный двор",
        )
        self.assertIsNone(wikipedia_title_to_station_label("Список станций Петербургского метрополитена"))

    def test_norm_metro_station_key(self) -> None:
        self.assertEqual(
            norm_metro_station_key("м. Невский проспект"),
            norm_metro_station_key("Невский проспект"),
        )

    def test_fetch_spb_wikipedia_count(self) -> None:
        stations = fetch_spb_metro_stations_from_wikipedia()
        self.assertGreaterEqual(len(stations), 70)
        self.assertIn("Невский проспект", stations)
        self.assertIn("Площадь Восстания", stations)

    def test_msk_district_title_cleanup(self) -> None:
        self.assertEqual(
            wikipedia_title_to_msk_district_label("Тверской район (Москва)"),
            "Тверской район",
        )
        self.assertEqual(wikipedia_title_to_msk_district_label("Хамовники"), "Хамовники")

    def test_fetch_msk_wikipedia_counts(self) -> None:
        districts = fetch_msk_districts_from_wikipedia()
        self.assertGreaterEqual(len(districts), 125)
        self.assertIn("Тверской район", districts)
        self.assertIn("Хамовники", districts)
        stations = fetch_msk_metro_stations_from_wikipedia()
        self.assertGreaterEqual(len(stations), 200)
        self.assertIn("Тверская", stations)
        self.assertIn("Киевская", stations)


if __name__ == "__main__":
    unittest.main()
