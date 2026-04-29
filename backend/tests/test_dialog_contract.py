from __future__ import annotations

import pathlib
import sys
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.routers import dialog


class _StubGraphRunner:
    async def run_dialog(self, messages, session_id, client_action=None):
        _ = (messages, session_id, client_action)
        return {
            "reply": "ok",
            "session_id": "s-1",
            "state": {
                "context": {
                    "booking_pending": True,
                    "booking_complete": False,
                    "booking_requirements": {
                        "starts_at": "2026-04-14T20:00:00+03:00",
                        "guest_count": 4,
                        "guest_name": "Ivan",
                        "guest_phone": "+79990000000",
                    },
                }
            },
        }


async def _override_graph_runner():
    return _StubGraphRunner()


class DialogContractTest(unittest.TestCase):
    def test_dialog_response_shape_is_preserved(self) -> None:
        app = FastAPI()
        app.include_router(dialog.router, prefix="/api")
        app.dependency_overrides[dialog.get_graph_runner] = _override_graph_runner
        client = TestClient(app)

        resp = client.post(
            "/api/dialog",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "client_action": {
                    "type": "submit_booking",
                    "starts_at": "2026-04-14T20:00:00+03:00",
                    "guest_count": 4,
                    "guest_name": "Ivan",
                    "guest_phone": "+79990000000",
                },
            },
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("reply", payload)
        self.assertIn("session_id", payload)
        self.assertIn("state", payload)
        self.assertIn("context", payload["state"])


if __name__ == "__main__":
    unittest.main()

