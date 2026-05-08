from __future__ import annotations

import pathlib
import sys
import unittest
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.routers import dialog


class _SpecificNotFoundGraphRunner:
    async def run_dialog(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str],
        client_action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = (messages, session_id, client_action)
        return {
            "reply": (
                "Не удалось однозначно найти этот ресторан. "
                "Уточните, пожалуйста, адрес/район или пришлите ссылку на карточку."
            ),
            "session_id": "s-1",
            "state": {
                "context": {
                    "booking_intent_mode": "specific_restaurant",
                    "specific_restaurant_resolved": False,
                    "booking_pending": False,
                    "booking_complete": False,
                    "booking_selected_candidate": {},
                    "recommendations": [],
                }
            },
        }


async def _override_graph_runner():
    return _SpecificNotFoundGraphRunner()


class DialogSpecificNotFoundFlowTest(unittest.TestCase):
    def test_not_found_requires_more_specific_input(self) -> None:
        app = FastAPI()
        app.include_router(dialog.router, prefix="/api")
        app.dependency_overrides[dialog.get_graph_runner] = _override_graph_runner
        client = TestClient(app)

        resp = client.post(
            "/api/dialog",
            json={
                "messages": [{"role": "user", "content": "Забронируй столик в Ресторане Мечта в Москве"}],
            },
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("Уточните", payload.get("reply", ""))
        ctx = payload["state"]["context"]

        self.assertEqual(ctx.get("booking_intent_mode"), "specific_restaurant")
        self.assertFalse(ctx.get("specific_restaurant_resolved"))
        self.assertFalse(ctx.get("booking_pending"))
        self.assertFalse(ctx.get("booking_complete"))
        self.assertEqual(ctx.get("booking_selected_candidate"), {})
        self.assertEqual(ctx.get("recommendations"), [])


if __name__ == "__main__":
    unittest.main()
