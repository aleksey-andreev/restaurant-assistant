from __future__ import annotations

from typing import Any, Dict


def _first_str(*values: Any) -> str:
    for v in values:
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def build_receipt_booking_snapshot(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Снимок полей брони для PDF — не зависит от последующих изменений контекста."""
    sel = ctx.get("booking_selected_candidate") if isinstance(ctx.get("booking_selected_candidate"), dict) else {}
    resv = ctx.get("reservation_result") if isinstance(ctx.get("reservation_result"), dict) else {}
    req = ctx.get("booking_requirements") if isinstance(ctx.get("booking_requirements"), dict) else {}
    try:
        guest_count = int(ctx.get("preorder_guest_count") or resv.get("guest_count") or req.get("guest_count") or 1)
    except (TypeError, ValueError):
        guest_count = 1
    guest_count = max(1, min(1000, guest_count))
    return {
        "restaurant_name": _first_str(sel.get("name"), resv.get("restaurant_name"), resv.get("name")),
        "restaurant_address": _first_str(
            sel.get("address"), resv.get("restaurant_address"), resv.get("address")
        ),
        "starts_at": _first_str(resv.get("starts_at"), req.get("starts_at")),
        "table_title": _first_str(resv.get("table_title")),
        "guest_name": _first_str(resv.get("guest_name"), req.get("guest_name")),
        "guest_phone": _first_str(resv.get("guest_phone"), req.get("guest_phone")),
        "guest_count": guest_count,
    }
