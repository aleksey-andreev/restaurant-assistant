"""Unit tests for Afisha restaurant sitemap discovery."""

from __future__ import annotations

import unittest

from app.services.afisha_catalog_sitemap import _pick_restaurant_leaf_sitemaps


class AfishaCatalogSitemapTest(unittest.TestCase):
    def test_pick_restaurant_leaf_from_index(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://www.afisha.ru/spb/restaurants/sitemap-afisha_choice.xml</loc>
  </sitemap>
  <sitemap>
    <loc>https://www.afisha.ru/spb/restaurants/sitemap-restuarants.xml</loc>
  </sitemap>
</sitemapindex>
"""
        picked = _pick_restaurant_leaf_sitemaps(xml, "spb")
        self.assertEqual(
            picked,
            ["https://www.afisha.ru/spb/restaurants/sitemap-restuarants.xml"],
        )


if __name__ == "__main__":
    unittest.main()
