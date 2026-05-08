"""Lightweight tests for OSM geo helpers (no live HTTP)."""

from __future__ import annotations

import unittest

from app.services.osm_geo import (
    OsmGeoResult,
    _extract_spb_district_from_yandex_raw,
    _normalize_metro_station_name,
    _normalize_spb_official_district,
)


class OsmGeoResultTest(unittest.TestCase):
    def test_ok_when_district_only(self) -> None:
        r = OsmGeoResult("Центральный", [], None, 59.93, 30.31)
        self.assertTrue(r.ok)

    def test_ok_when_metros_only(self) -> None:
        r = OsmGeoResult(None, ["Невский проспект", "Гостиный двор"], "Невский проспект", 59.93, 30.31)
        self.assertTrue(r.ok)

    def test_not_ok_when_empty(self) -> None:
        r = OsmGeoResult(None, [], None, None, None)
        self.assertFalse(r.ok)

    def test_normalize_spb_official_district_from_city_district(self) -> None:
        addr = {"city_district": "Петроградский район", "suburb": "Посадский округ"}
        got = _normalize_spb_official_district(addr)
        self.assertEqual(got, "Петроградский район")

    def test_normalize_spb_drops_municipal_okrug_only(self) -> None:
        addr = {"suburb": "Дворцовый округ", "quarter": "округ № 78"}
        got = _normalize_spb_official_district(addr)
        self.assertIsNone(got)

    def test_normalize_metro_station_name(self) -> None:
        self.assertEqual(_normalize_metro_station_name("станция Невский проспект"), "Невский проспект")
        self.assertEqual(_normalize_metro_station_name("м. Гостиный двор"), "Гостиный двор")

    def test_extract_spb_district_from_yandex_raw(self) -> None:
        raw = (
            "<passage>Муниципальный округ №78, Центральный район, Санкт-Петербург.</passage>"
            "<extended-text>Адрес: Невский проспект, 35</extended-text>"
        )
        self.assertEqual(_extract_spb_district_from_yandex_raw(raw), "Центральный район")

    def test_extract_spb_district_from_yandex_raw_ambiguous(self) -> None:
        raw = "<passage>Центральный район.</passage><passage>Адмиралтейский район.</passage>"
        self.assertIsNone(_extract_spb_district_from_yandex_raw(raw))


if __name__ == "__main__":
    unittest.main()
