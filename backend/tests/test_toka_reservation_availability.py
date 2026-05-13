from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.toka_mcp_agent import (  # noqa: E402
    _dates_for_reservation_fetch,
    _max_blocking_end_utc,
    _pick_smallest_free_table_id,
    _status_blocks_table,
    _table_available_for_interval,
    _toka_list_date_str,
)


class TokaReservationAvailabilityTest(unittest.TestCase):
    def test_status_blocks_table(self) -> None:
        self.assertFalse(_status_blocks_table("completed"))
        self.assertFalse(_status_blocks_table("cancelled"))
        self.assertFalse(_status_blocks_table("canceled"))
        self.assertFalse(_status_blocks_table("no_show"))
        self.assertFalse(_status_blocks_table("no-show"))
        self.assertTrue(_status_blocks_table("confirmed"))
        self.assertTrue(_status_blocks_table("seated"))
        self.assertTrue(_status_blocks_table(None))
        self.assertTrue(_status_blocks_table("pending"))

    def test_dates_for_cross_midnight_interval(self) -> None:
        z = timezone.utc
        s = datetime(2026, 5, 8, 23, 0, tzinfo=z)
        e = datetime(2026, 5, 9, 1, 0, tzinfo=z)
        days = _dates_for_reservation_fetch(s, e)
        self.assertEqual(days, ["2026-05-08", "2026-05-09"])

    def test_pick_smallest_skips_occupied_table(self) -> None:
        z = timezone.utc
        halls = {
            "items": [
                {
                    "tables": [
                        {"id": "t2", "capacity": 2},
                        {"id": "t4", "capacity": 4},
                    ]
                }
            ]
        }
        new_s = datetime(2026, 5, 8, 12, 0, tzinfo=z)
        new_e = new_s + timedelta(hours=2)
        reservations = [
            {
                "id": "r1",
                "table_id": "t2",
                "starts_at": "2026-05-08T12:00:00Z",
                "ends_at": "2026-05-08T14:00:00Z",
                "status": "confirmed",
            },
        ]
        tid = _pick_smallest_free_table_id(halls, 2, new_s, new_e, reservations)
        self.assertEqual(tid, "t4")

    def test_cancelled_ignored(self) -> None:
        z = timezone.utc
        halls = {"items": [{"tables": [{"id": "t2", "capacity": 2}]}]}
        new_s = datetime(2026, 5, 8, 12, 0, tzinfo=z)
        new_e = new_s + timedelta(hours=2)
        reservations = [
            {
                "id": "r1",
                "table_id": "t2",
                "starts_at": "2026-05-08T12:00:00Z",
                "ends_at": "2026-05-08T14:00:00Z",
                "status": "cancelled",
            },
        ]
        tid = _pick_smallest_free_table_id(halls, 2, new_s, new_e, reservations)
        self.assertEqual(tid, "t2")

    def test_completed_ignored(self) -> None:
        z = timezone.utc
        halls = {"items": [{"tables": [{"id": "t2", "capacity": 2}]}]}
        new_s = datetime(2026, 5, 8, 12, 0, tzinfo=z)
        new_e = new_s + timedelta(hours=2)
        reservations = [
            {
                "id": "r1",
                "table_id": "t2",
                "starts_at": "2026-05-08T12:00:00Z",
                "ends_at": "2026-05-08T14:00:00Z",
                "status": "completed",
            },
        ]
        tid = _pick_smallest_free_table_id(halls, 2, new_s, new_e, reservations)
        self.assertEqual(tid, "t2")

    def test_adjacent_slots_do_not_overlap(self) -> None:
        z = timezone.utc
        new_s = datetime(2026, 5, 8, 14, 30, tzinfo=z)
        new_e = new_s + timedelta(hours=2)
        reservations = [
            {
                "table_id": "t2",
                "starts_at": "2026-05-08T12:00:00Z",
                "ends_at": "2026-05-08T14:30:00Z",
                "status": "confirmed",
            },
        ]
        self.assertTrue(
            _table_available_for_interval("t2", new_s, new_e, reservations)
        )

    def test_toka_list_date_str_uses_client_zone(self) -> None:
        self.assertEqual(_toka_list_date_str("2026-05-09T01:00:00Z", "Europe/Moscow"), "2026-05-09")

    def test_toka_list_date_str_fallback_utc(self) -> None:
        self.assertEqual(_toka_list_date_str("2026-05-09T01:00:00Z", None), "2026-05-09")

    def test_max_blocking_end_overlapping(self) -> None:
        z = timezone.utc
        new_s = datetime(2026, 5, 8, 12, 0, tzinfo=z)
        new_e = new_s + timedelta(hours=2)
        reservations = [
            {
                "table_id": "t2",
                "starts_at": "2026-05-08T11:00:00Z",
                "ends_at": "2026-05-08T13:30:00Z",
                "status": "confirmed",
            },
        ]
        end = _max_blocking_end_utc("t2", new_s, new_e, reservations)
        self.assertIsNotNone(end)
        assert end is not None
        self.assertEqual(end, datetime(2026, 5, 8, 13, 30, tzinfo=z))


if __name__ == "__main__":
    unittest.main()
