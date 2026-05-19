"""Preorder LLM pick is logged to pipeline_events."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock

from app.services.preorder_dialog import (
    PREORDER_PIPELINE_STAGE_LLM_PICK,
    PreorderLlmPickOutcome,
    _record_preorder_llm_pick_event,
)


class _FakeStateRepository:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def append_pipeline_events(
        self,
        session_id: str,
        batch_id: str,
        events: List[Dict[str, Any]],
    ) -> None:
        self.calls.append(
            {"session_id": session_id, "batch_id": batch_id, "events": events}
        )


class TestPreorderPipelineEvents(unittest.TestCase):
    def test_record_preorder_llm_pick_event_shape(self) -> None:
        repo = _FakeStateRepository()
        outcome = PreorderLlmPickOutcome(
            picks=[{"menu_item_id": "id-1", "quantity": 2}],
            raw='{"items":[{"menu_item_id":"id-1","quantity":2}]}',
            ok=True,
            error=None,
            model="zai-org/GLM-4.7",
        )
        lines = [
            {
                "menu_item_id": "id-1",
                "quantity": 2,
                "title": "Суп",
                "price": 300.0,
                "line_total": 600.0,
                "section": "Меню",
            }
        ]

        asyncio.run(
            _record_preorder_llm_pick_event(
                repo,  # type: ignore[arg-type]
                "sess-abc",
                source="mode_choice",
                preferences="диетическое до 1000",
                guest_count=2,
                menu_positions_count=42,
                outcome=outcome,
                cart_lines=lines,
            )
        )

        self.assertEqual(len(repo.calls), 1)
        call = repo.calls[0]
        self.assertEqual(call["session_id"], "sess-abc")
        self.assertTrue(call["batch_id"])

        ev = call["events"][0]
        self.assertEqual(ev["stage"], PREORDER_PIPELINE_STAGE_LLM_PICK)
        self.assertIn("ts", ev)
        p = ev["payload"]
        self.assertEqual(p["source"], "mode_choice")
        self.assertEqual(p["preferences"], "диетическое до 1000")
        self.assertEqual(p["guest_count"], 2)
        self.assertEqual(p["menu_positions_count"], 42)
        self.assertEqual(p["model"], "zai-org/GLM-4.7")
        self.assertTrue(p["ok"])
        self.assertIsNone(p["error"])
        self.assertIn("items", p["raw"])
        self.assertEqual(p["picks"], outcome.picks)
        self.assertEqual(p["cart_lines_count"], 1)
        self.assertEqual(p["cart_lines"][0]["title"], "Суп")


if __name__ == "__main__":
    unittest.main()
