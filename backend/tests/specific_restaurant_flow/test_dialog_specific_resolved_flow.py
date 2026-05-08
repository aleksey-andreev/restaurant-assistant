from __future__ import annotations

import pathlib
import sys
import unittest
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.routers import dialog


class _SpecificResolvedGraphRunner:
    async def run_dialog(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str],
        client_action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = (messages, session_id, client_action)
        selected = {
            "name": "White Rabbit",
            "url": "https://afisha.ru/moscow/restaurant/white-rabbit/",
            "address": "Смоленская площадь, 3",
        }
        return {
            "reply": "Нашёл нужный ресторан. Заполните форму бронирования ниже.",
            "session_id": "s-1",
            "state": {
                "context": {
                    "booking_intent_mode": "specific_restaurant",
                    "specific_restaurant_resolved": True,
                    "booking_pending": True,
                    "booking_complete": False,
                    "booking_selected_candidate": selected,
                    "booking_missing_fields": ["starts_at", "guest_count", "guest_name", "guest_phone"],
                    "recommendations": [selected],
                }
            },
        }


async def _override_graph_runner():
    return _SpecificResolvedGraphRunner()


class DialogSpecificResolvedFlowTest(unittest.TestCase):
    def test_resolved_flow_opens_booking_without_candidate_selection(self) -> None:
        app = FastAPI()
        app.include_router(dialog.router, prefix="/api")
        app.dependency_overrides[dialog.get_graph_runner] = _override_graph_runner
        client = TestClient(app)

        resp = client.post(
            "/api/dialog",
            json={
                "messages": [{"role": "user", "content": "Забронируй столик в White Rabbit в Москве"}],
            },
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        ctx = payload["state"]["context"]

        self.assertEqual(ctx.get("booking_intent_mode"), "specific_restaurant")
        self.assertTrue(ctx.get("specific_restaurant_resolved"))
        self.assertTrue(ctx.get("booking_pending"))
        self.assertFalse(ctx.get("booking_complete"))
        self.assertEqual(ctx.get("booking_selected_candidate", {}).get("name"), "White Rabbit")
        self.assertEqual(
            ctx.get("booking_missing_fields"),
            ["starts_at", "guest_count", "guest_name", "guest_phone"],
        )
        self.assertEqual(len(ctx.get("recommendations") or []), 1)


if __name__ == "__main__":
    unittest.main()
