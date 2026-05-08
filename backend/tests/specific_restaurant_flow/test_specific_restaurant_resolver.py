from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.services.toka_specific_restaurant_resolver import resolve_specific_restaurant_candidates


class _StubSearchClient:
    def __init__(self, urls):
        self._urls = list(urls)

    async def search(self, query: str, page: int = 0, max_docs: int = 30):
        _ = (query, page, max_docs)
        return list(self._urls)


class SpecificRestaurantResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_when_single_strong_match(self) -> None:
        parsed = [
            {
                "name": "White Rabbit",
                "url": "https://afisha.ru/moscow/restaurant/white-rabbit/",
                "address": "Смоленская площадь, 3",
            },
            {"name": "Sage", "url": "https://afisha.ru/moscow/restaurant/sage/", "address": "1-я Тверская"},
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.YandexWebSearchClient.from_env",
                return_value=_StubSearchClient(["u1", "u2"]),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.filter_and_order_afisha_restaurant_urls",
                return_value=["u1", "u2"],
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.fetch_and_parse_afisha_card",
                new=AsyncMock(side_effect=parsed),
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="moscow",
                restaurant_name="White Rabbit",
            )

        self.assertEqual(out["status"], "resolved")
        self.assertIsInstance(out.get("selected"), dict)
        self.assertEqual(out["selected"].get("name"), "White Rabbit")

    async def test_ambiguous_when_multiple_name_matches(self) -> None:
        parsed = [
            {"name": "Сыроварня на Красном Октябре", "url": "u1", "address": "Берсеневская наб."},
            {"name": "Сыроварня на Усачевском", "url": "u2", "address": "ул. Усачева"},
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.YandexWebSearchClient.from_env",
                return_value=_StubSearchClient(["u1", "u2"]),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.filter_and_order_afisha_restaurant_urls",
                return_value=["u1", "u2"],
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.fetch_and_parse_afisha_card",
                new=AsyncMock(side_effect=parsed),
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="moscow",
                restaurant_name="Сыроварня",
            )

        self.assertEqual(out["status"], "ambiguous")
        self.assertIsNone(out.get("selected"))
        self.assertGreaterEqual(len(out.get("candidates") or []), 2)

    async def test_not_found_when_no_cards_parsed(self) -> None:
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.YandexWebSearchClient.from_env",
                return_value=_StubSearchClient([]),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.filter_and_order_afisha_restaurant_urls",
                return_value=[],
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.fetch_and_parse_afisha_card",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.get_session_maker",
                return_value=lambda: None,
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.AfishaCatalogRepository.find_rows_for_city_by_name_like",
                return_value=[],
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="moscow",
                restaurant_name="Unknown Place",
            )

        self.assertEqual(out["status"], "not_found")
        self.assertEqual(out.get("candidates"), [])

    async def test_resolved_from_db_fallback_when_yandex_not_found(self) -> None:
        db_candidates = [
            {
                "name": "Unknown Place",
                "url": "https://afisha.ru/moscow/restaurant/unknown-place/",
                "address": "Some address",
                "venue_closed": False,
            }
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.YandexWebSearchClient.from_env",
                return_value=_StubSearchClient([]),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.filter_and_order_afisha_restaurant_urls",
                return_value=[],
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.fetch_and_parse_afisha_card",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.get_session_maker",
                return_value=lambda: None,
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.AfishaCatalogRepository.find_rows_for_city_by_name_like",
                return_value=db_candidates,
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="moscow",
                restaurant_name="Unknown Place",
            )

        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["selected"].get("name"), "Unknown Place")

    async def test_db_fallback_skips_candidates_without_address(self) -> None:
        # Even if DB has a name match, a candidate without address must not be used.
        db_candidates = [
            {
                "name": "Unknown Place",
                "url": "https://afisha.ru/moscow/restaurant/unknown-place/",
                "address": "",
                "venue_closed": False,
            }
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.YandexWebSearchClient.from_env",
                return_value=_StubSearchClient([]),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.filter_and_order_afisha_restaurant_urls",
                return_value=[],
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.fetch_and_parse_afisha_card",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.get_session_maker",
                return_value=lambda: None,
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.AfishaCatalogRepository.find_rows_for_city_by_name_like",
                return_value=db_candidates,
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="moscow",
                restaurant_name="Unknown Place",
            )

        self.assertEqual(out["status"], "not_found")
        self.assertEqual(out.get("candidates"), [])

    async def test_skips_candidates_without_address_and_junk_names(self) -> None:
        parsed = [
            {"name": "Империя, отзывы", "url": "u1", "address": ""},
            {"name": "Империя", "url": "u2"},
            {"name": "Империя", "url": "u3", "address": "Тверская, 7"},
        ]
        with (
            patch(
                "app.services.toka_specific_restaurant_resolver.YandexWebSearchClient.from_env",
                return_value=_StubSearchClient(["u1", "u2", "u3"]),
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.filter_and_order_afisha_restaurant_urls",
                return_value=["u1", "u2", "u3"],
            ),
            patch(
                "app.services.toka_specific_restaurant_resolver.fetch_and_parse_afisha_card",
                new=AsyncMock(side_effect=parsed),
            ),
        ):
            out = await resolve_specific_restaurant_candidates(
                city_slug="moscow",
                restaurant_name="Империя",
            )

        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["selected"].get("url"), "u3")


if __name__ == "__main__":
    unittest.main()
