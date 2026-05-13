from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.toka_client import find_table_title  # noqa: E402


class FindTableTitleTest(unittest.TestCase):
    def test_prefers_title(self) -> None:
        halls = {"items": [{"tables": [{"id": "a1", "capacity": 2, "title": "У окна"}]}]}
        self.assertEqual(find_table_title(halls, "a1"), "У окна")

    def test_fallback_name(self) -> None:
        halls = {"items": [{"tables": [{"id": "b2", "capacity": 4, "name": "VIP"}]}]}
        self.assertEqual(find_table_title(halls, "b2"), "VIP")

    def test_fallback_id(self) -> None:
        halls = {"items": [{"tables": [{"id": "c3", "capacity": 2}]}]}
        self.assertEqual(find_table_title(halls, "c3"), "Стол c3")


if __name__ == "__main__":
    unittest.main()
