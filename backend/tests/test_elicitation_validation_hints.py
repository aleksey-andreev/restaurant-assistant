"""Tests for elicitation validation feedback (collect → validate context)."""

from __future__ import annotations

import unittest

from app.services.graph_runner import (
    build_elicitation_complete_user_reply,
    build_elicitation_user_reply,
    build_elicitation_validation_feedback_block,
    compute_elicitation_validation_hints,
    elicitation_reply_echoes_user_utterance,
    merge_elicitation_llm_dicts,
    parse_elicitation_llm_json,
    pick_elicitation_user_reply,
    validate_recommendation_requirements_fields,
)


class TestElicitationValidationHints(unittest.TestCase):
    def test_named_restaurant_missing_city_only(self) -> None:
        req = {"intent": "named_restaurant", "restaurant_name": "Эвкалипт"}
        missing = validate_recommendation_requirements_fields(req)
        self.assertEqual(missing, ["city"])

    def test_unresolved_city_after_prior_ask(self) -> None:
        prior = {"text": "В каком городе?", "asked_slots": ["city"]}
        req = {"intent": "named_restaurant", "restaurant_name": "Эвкалипт"}
        missing, unresolved, not_yet = compute_elicitation_validation_hints(req, prior)
        self.assertIn("city", missing)
        self.assertEqual(unresolved, ["city"])
        self.assertEqual(not_yet, [])

    def test_not_yet_city_if_never_asked(self) -> None:
        req = {"intent": "named_restaurant", "restaurant_name": "Эвкалипт"}
        missing, unresolved, not_yet = compute_elicitation_validation_hints(req, {})
        self.assertIn("city", missing)
        self.assertEqual(unresolved, [])
        self.assertIn("city", not_yet)

    def test_complete_after_canonical_city(self) -> None:
        req = {
            "intent": "named_restaurant",
            "restaurant_name": "Эвкалипт",
            "city": "Санкт-Петербург",
            "city_slug": "spb",
        }
        missing, unresolved, not_yet = compute_elicitation_validation_hints(
            req, {"text": "Город?", "asked_slots": ["city"]}
        )
        self.assertEqual(missing, [])
        self.assertEqual(unresolved, [])
        self.assertEqual(not_yet, [])

    def test_parse_json_after_redacted_thinking(self) -> None:
        ot, ct = "<think>", "</think>"
        raw = (
            f"{ot}думаю{ct}"
            '{"intent":"named_restaurant","restaurant_name":"Эвкалипт",'
            '"city":"Санкт-Петербург","user_reply":"Понял, ищем в Петербурге.","asked_slots":[]}'
        )
        parsed = parse_elicitation_llm_json(raw)
        self.assertEqual(parsed.get("city"), "Санкт-Петербург")
        self.assertTrue(parsed.get("user_reply"))

    def test_merge_llm_dicts_keeps_primary_city(self) -> None:
        first = {"city": "Санкт-Петербург", "user_reply": ""}
        second = {"user_reply": "Хорошо!"}
        merged = merge_elicitation_llm_dicts(first, second)
        self.assertEqual(merged.get("city"), "Санкт-Петербург")
        self.assertEqual(merged.get("user_reply"), "Хорошо!")

    def test_elicitation_echo_detection(self) -> None:
        u = "Встречаюсь с друзьями в Питере, подбери что-нибудь в центре"
        self.assertTrue(elicitation_reply_echoes_user_utterance(u, u))
        self.assertTrue(elicitation_reply_echoes_user_utterance(f"  {u}  ", u))
        self.assertFalse(elicitation_reply_echoes_user_utterance("Понял, ищем в центре Петербурга.", u))

    def test_pick_reply_prefers_llm_when_not_echo(self) -> None:
        user = "Забронируй ресторан Ипполит"
        parsed = {
            "intent": "named_restaurant",
            "restaurant_name": "Ипполит",
            "user_reply": "Хорошо, уточните город для Ипполит.",
            "asked_slots": ["city"],
        }
        req = {"intent": "named_restaurant", "restaurant_name": "Ипполит"}
        reply, source, slots = pick_elicitation_user_reply(
            parsed=parsed,
            last_user_text=user,
            req_complete=False,
            new_req=req,
            missing_fb=["city"],
            unresolved_fb=[],
            not_yet_fb=["city"],
        )
        self.assertEqual(source, "llm_question")
        self.assertIn("Ипполит", reply)
        self.assertEqual(slots, ["city"])

    def test_pick_reply_falls_back_to_template_on_echo(self) -> None:
        user = "Забронируй ресторан Ипполит"
        parsed = {
            "intent": "named_restaurant",
            "restaurant_name": "Ипполит",
            "user_reply": user,
            "asked_slots": ["city"],
        }
        req = {"intent": "named_restaurant", "restaurant_name": "Ипполит"}
        reply, source, _ = pick_elicitation_user_reply(
            parsed=parsed,
            last_user_text=user,
            req_complete=False,
            new_req=req,
            missing_fb=["city"],
            unresolved_fb=[],
            not_yet_fb=["city"],
        )
        self.assertEqual(source, "template_question")
        self.assertIn("Записал", reply)

    def test_user_reply_named_restaurant_asks_city(self) -> None:
        req = {"intent": "named_restaurant", "restaurant_name": "Ипполит"}
        reply = build_elicitation_user_reply(
            missing=["city"],
            unresolved=[],
            not_yet=["city"],
            req=req,
        )
        self.assertIn("Ипполит", reply)
        self.assertIn("город", reply.lower())

    def test_complete_reply_named_restaurant(self) -> None:
        req = {
            "intent": "named_restaurant",
            "restaurant_name": "Ипполит",
            "city": "Санкт-Петербург",
        }
        reply = build_elicitation_complete_user_reply(req)
        self.assertIn("Ипполит", reply)
        self.assertIn("Санкт-Петербург", reply)

    def test_feedback_block_mentions_unresolved(self) -> None:
        prior = {"text": "В каком городе?", "asked_slots": ["city"]}
        block = build_elicitation_validation_feedback_block(
            missing=["city"],
            unresolved=["city"],
            not_yet=[],
            elicitation_prior=prior,
            last_user_text="Питер",
        )
        self.assertIn("уже отвечал", block)
        self.assertIn("city", block)
        self.assertNotIn('"Питер"', block)


if __name__ == "__main__":
    unittest.main()
