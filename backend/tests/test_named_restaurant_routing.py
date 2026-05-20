"""Routing helpers for named-restaurant elicitation and resolve deferral."""

from __future__ import annotations

import json
import unittest

from app.services.graph_runner import (
    fingerprint_search_plan,
    named_restaurant_not_found_reply,
    should_defer_named_restaurant_resolve,
    sync_search_plan_confirm_after_req_merge,
)


class NamedRestaurantRoutingTest(unittest.TestCase):
    def test_fingerprint_changes_when_restaurant_name_changes(self) -> None:
        a = fingerprint_search_plan(
            {"intent": "named_restaurant", "city": "Москва", "restaurant_name": "Боливуд"}
        )
        b = fingerprint_search_plan(
            {"intent": "named_restaurant", "city": "Москва", "restaurant_name": "Bollywood"}
        )
        self.assertNotEqual(a, b)

    def test_fingerprint_ignores_address_hint(self) -> None:
        a = fingerprint_search_plan(
            {
                "intent": "named_restaurant",
                "city": "Москва",
                "restaurant_name": "X",
                "address_or_hint": "центр",
            }
        )
        b = fingerprint_search_plan(
            {"intent": "named_restaurant", "city": "Москва", "restaurant_name": "X"}
        )
        self.assertEqual(a, b)
        self.assertNotIn("hint", json.loads(a))

    def test_sync_clears_confirm_on_fingerprint_change(self) -> None:
        req = {"intent": "named_restaurant", "city": "Москва", "restaurant_name": "Bollywood"}
        old_fp = fingerprint_search_plan(
            {"intent": "named_restaurant", "city": "Москва", "restaurant_name": "Боливуд"}
        )
        confirmed, new_fp = sync_search_plan_confirm_after_req_merge(
            had_confirmed=True,
            old_fingerprint=old_fp,
            new_req=req,
        )
        self.assertFalse(confirmed)
        self.assertEqual(new_fp, fingerprint_search_plan(req))

    def test_sync_keeps_confirm_when_fingerprint_unchanged(self) -> None:
        req = {"intent": "named_restaurant", "city": "Москва", "restaurant_name": "Боливуд"}
        fp = fingerprint_search_plan(req)
        confirmed, new_fp = sync_search_plan_confirm_after_req_merge(
            had_confirmed=True,
            old_fingerprint=fp,
            new_req=req,
        )
        self.assertTrue(confirmed)
        self.assertEqual(new_fp, fp)

    def test_defer_when_city_still_missing(self) -> None:
        req = {"intent": "named_restaurant", "restaurant_name": "Боливуд"}
        state = {"last_elicitation": {"asked_slots": ["city"]}}
        self.assertTrue(should_defer_named_restaurant_resolve(state, req))

    def test_no_defer_when_requirements_complete(self) -> None:
        req = {
            "intent": "named_restaurant",
            "city": "Москва",
            "city_slug": "msk",
            "restaurant_name": "Bollywood",
        }
        state = {"last_elicitation": {"asked_slots": []}}
        self.assertFalse(should_defer_named_restaurant_resolve(state, req))

    def test_not_found_reply_first_attempt(self) -> None:
        msg = named_restaurant_not_found_reply(
            {"restaurant_name": "Bollywood", "city": "Москва"},
            resolve_attempts=1,
        )
        self.assertIn("каталоге", msg)
        self.assertIn("Bollywood", msg)
        self.assertNotIn("ссылк", msg.lower())

    def test_not_found_reply_repeat_suggests_search(self) -> None:
        msg = named_restaurant_not_found_reply(
            {"restaurant_name": "Bollywood", "city": "Москва"},
            resolve_attempts=2,
        )
        self.assertIn("похожие", msg)


if __name__ == "__main__":
    unittest.main()
