"""Reference-only city_slug resolution (no slang, no transliteration)."""

import unittest

from app.services.afisha_city_slug import resolve_afisha_city_slug


class TestAfishaCitySlugReference(unittest.TestCase):
    def test_canonical_ru(self) -> None:
        self.assertEqual(resolve_afisha_city_slug("Санкт-Петербург"), "spb")
        self.assertEqual(resolve_afisha_city_slug("Москва"), "msk")
        self.assertEqual(resolve_afisha_city_slug("Владивосток"), "vladivostok")

    def test_slang_not_mapped(self) -> None:
        self.assertIsNone(resolve_afisha_city_slug("Питер"))
        self.assertIsNone(resolve_afisha_city_slug("Владик"))
        self.assertIsNone(resolve_afisha_city_slug("СПб"))
        self.assertIsNone(resolve_afisha_city_slug("петербург"))

    def test_direct_slug(self) -> None:
        self.assertEqual(resolve_afisha_city_slug("spb"), "spb")


if __name__ == "__main__":
    unittest.main()
