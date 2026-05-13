from __future__ import annotations

import pathlib
import sys
import unittest
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.routers import dialog


def _make_candidate(name: str, url: str, address: str) -> Dict[str, Any]:
    return {"name": name, "url": url, "address": address}


class _SpecificAmbiguousGraphRunner:
    async def run_dialog(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str],
        client_action: Optional[Dict[str, Any]] = None,
        client_time_zone: Optional[str] = None,
    ) -> Dict[str, Any]:
        _ = (messages, session_id, client_time_zone)
        candidates = [
            _make_candidate("Сыроварня на Красном Октябре", "https://afisha.ru/u1", "Берсеневская наб."),
            _make_candidate("Сыроварня на Усачевском", "https://afisha.ru/u2", "ул. Усачева"),
        ]
        if client_action and client_action.get("type") == "select_booking_candidate":
            idx = int(client_action.get("index") or 0)
            chosen = candidates[idx]
            return {
                "reply": "Ресторан выбран. Заполните форму бронирования ниже.",
                "session_id": "s-1",
                "state": {
                    "context": {
                        "booking_intent_mode": "specific_restaurant",
                        "booking_pending": True,
                        "booking_complete": False,
                        "booking_selected_candidate": chosen,
                        "booking_missing_fields": ["starts_at", "guest_count", "guest_name", "guest_phone"],
                        "recommendations": candidates,
                    }
                },
            }

        return {
            "reply": "Нашёл несколько похожих ресторанов. Выберите нужный вариант в карточках ниже.",
            "session_id": "s-1",
            "state": {
                "context": {
                    "booking_intent_mode": "specific_restaurant",
                    "booking_pending": True,
                    "booking_complete": False,
                    "booking_selected_candidate": {},
                    "recommendations": candidates,
                }
            },
        }


async def _override_graph_runner():
    return _SpecificAmbiguousGraphRunner()


class DialogSpecificAmbiguousFlowTest(unittest.TestCase):
    def test_ambiguous_then_select_candidate_keeps_booking_pending(self) -> None:
        app = FastAPI()
        app.include_router(dialog.router, prefix="/api")
        app.dependency_overrides[dialog.get_graph_runner] = _override_graph_runner
        client = TestClient(app)

        first = client.post(
            "/api/dialog",
            json={
                "messages": [{"role": "user", "content": "Забронируй столик в Сыроварне в Москве"}],
            },
        )
        self.assertEqual(first.status_code, 200)
        first_payload = first.json()
        first_ctx = first_payload["state"]["context"]
        self.assertTrue(first_ctx.get("booking_pending"))
        self.assertEqual(first_ctx.get("booking_intent_mode"), "specific_restaurant")
        self.assertEqual(first_ctx.get("booking_selected_candidate"), {})
        self.assertGreaterEqual(len(first_ctx.get("recommendations") or []), 2)

        second = client.post(
            "/api/dialog",
            json={
                "messages": [{"role": "user", "content": "Забронируй столик в Сыроварне в Москве"}],
                "client_action": {"type": "select_booking_candidate", "index": 1},
            },
        )
        self.assertEqual(second.status_code, 200)
        second_payload = second.json()
        second_ctx = second_payload["state"]["context"]
        self.assertTrue(second_ctx.get("booking_pending"))
        self.assertFalse(second_ctx.get("booking_complete"))
        self.assertEqual(second_ctx.get("booking_selected_candidate", {}).get("name"), "Сыроварня на Усачевском")
        self.assertEqual(
            second_ctx.get("booking_missing_fields"),
            ["starts_at", "guest_count", "guest_name", "guest_phone"],
        )


if __name__ == "__main__":
    unittest.main()
