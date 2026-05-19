"""Client context must not expose pipeline-only candidate diagnostics."""

from __future__ import annotations

import unittest

from app.services.graph_runner import sanitize_context_for_client, strip_candidate_for_client


class ClientContextSanitizeTest(unittest.TestCase):
    def test_strip_candidate_removes_toka_capacity_fields(self) -> None:
        raw = {
            "name": "Test",
            "url": "https://example.com/r",
            "toka_capacity_verified": False,
            "toka_capacity_message": "party_size not set — gate skipped",
            "formal_score": 0.8,
        }
        out = strip_candidate_for_client(raw)
        self.assertEqual(out["name"], "Test")
        self.assertEqual(out["formal_score"], 0.8)
        self.assertNotIn("toka_capacity_verified", out)
        self.assertNotIn("toka_capacity_message", out)

    def test_sanitize_context_strips_recommendation_lists(self) -> None:
        ctx = {
            "final_recommendations": [
                {
                    "name": "A",
                    "toka_capacity_message": "Не удалось подтвердить стол",
                }
            ],
            "booking_selected_candidate": {
                "name": "B",
                "toka_capacity_verified": True,
                "toka_capacity_message": "internal",
            },
            "recommendation_requirements": {"party_size": 2},
        }
        clean = sanitize_context_for_client(ctx)
        self.assertNotIn("toka_capacity_message", clean["final_recommendations"][0])
        self.assertNotIn("toka_capacity_verified", clean["booking_selected_candidate"])
        self.assertEqual(clean["recommendation_requirements"]["party_size"], 2)


if __name__ == "__main__":
    unittest.main()
