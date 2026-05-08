"""Tests for display city labels from Afisha slugs."""

from __future__ import annotations

import unittest

from app.services.afisha_city_slug import display_city_label_for_slug


class AfishaCitySlugDisplayTest(unittest.TestCase):
    def test_known_slugs(self) -> None:
        self.assertEqual(display_city_label_for_slug("spb"), "Санкт-Петербург")
        self.assertEqual(display_city_label_for_slug("msk"), "Москва")

    def test_unknown_slug_title(self) -> None:
        self.assertEqual(display_city_label_for_slug("perm"), "Perm")


if __name__ == "__main__":
    unittest.main()
