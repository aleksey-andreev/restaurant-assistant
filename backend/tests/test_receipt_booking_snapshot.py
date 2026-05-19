from app.services.receipt_booking_snapshot import build_receipt_booking_snapshot


def test_build_receipt_booking_snapshot_merges_sources() -> None:
    ctx = {
        "booking_selected_candidate": {
            "name": "Ресторан Тест",
            "address": "Санкт-Петербург, Невский пр., 1",
        },
        "reservation_result": {
            "starts_at": "2026-05-20T17:00:00.000Z",
            "table_title": "Стол 5",
            "guest_name": "Иван",
            "guest_phone": "+79001234567",
            "guest_count": 2,
        },
        "booking_requirements": {},
        "preorder_guest_count": 2,
    }
    snap = build_receipt_booking_snapshot(ctx)
    assert snap["restaurant_name"] == "Ресторан Тест"
    assert "Санкт-Петербург" in snap["restaurant_address"]
    assert snap["starts_at"] == "2026-05-20T17:00:00.000Z"
    assert snap["table_title"] == "Стол 5"
    assert snap["guest_phone"] == "+79001234567"
