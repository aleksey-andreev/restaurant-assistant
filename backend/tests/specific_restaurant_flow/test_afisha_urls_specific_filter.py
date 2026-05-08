from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.services.afisha_urls import filter_and_order_afisha_restaurant_urls


class AfishaUrlsSpecificFilterTest(unittest.TestCase):
    def test_keeps_only_canonical_restaurant_cards(self) -> None:
        urls = [
            "https://www.afisha.ru/msk/restaurant/imperia/",
            "https://www.afisha.ru/msk/restaurant/imperia/reviews/",
            "https://www.afisha.ru/msk/restaurant/imperia/menu/",
            "https://www.afisha.ru/msk/restaurant/imperia/?from=search",
            "https://www.afisha.ru/msk/theatre/something/",
            "https://example.com/msk/restaurant/imperia/",
        ]
        out = filter_and_order_afisha_restaurant_urls(urls)
        self.assertEqual(out, ["https://www.afisha.ru/msk/restaurant/imperia"])


if __name__ == "__main__":
    unittest.main()
