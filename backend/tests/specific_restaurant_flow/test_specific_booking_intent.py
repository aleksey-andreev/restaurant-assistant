from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.services.graph_runner import detect_specific_booking_intent


class SpecificBookingIntentTest(unittest.TestCase):
    def test_detects_direct_booking_request(self) -> None:
        self.assertTrue(
            detect_specific_booking_intent("Забронируй столик в White Rabbit в Москве на завтра")
        )

    def test_ignores_generic_discovery_search(self) -> None:
        self.assertFalse(
            detect_specific_booking_intent("Подбери ресторан в Москве по бюджету и кухне")
        )

    def test_ignores_empty_text(self) -> None:
        self.assertFalse(detect_specific_booking_intent(""))


if __name__ == "__main__":
    unittest.main()
