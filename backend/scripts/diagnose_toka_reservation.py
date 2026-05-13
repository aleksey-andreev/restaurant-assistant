#!/usr/bin/env python3
"""
Воспроизвести цепочку Toka для брони (как в TokaMcpAgent.toka_create_reservation) и увидеть ответ API.

Запуск из корня репозитория (подхватывается .env в корне):

  python3 backend/scripts/diagnose_toka_reservation.py

Параметры по умолчанию совпадают с последней отлаженной сессией (Ипполит, 13.05.2026 19:00 МСК).

Реальный POST в Toka (создаёт бронь) — только с флагом --post; без него выполняются
GET залов/столов, GET списка броней на дату, подбор стола и печать JSON-тела будущего POST.

Использует привязку из БД (toka_restaurant_bindings) и токены как в приложении.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--restaurant", default="Ипполит", help="Имя ресторана для lookup привязки")
    p.add_argument(
        "--starts-at",
        default="2026-05-13T16:00:00.000Z",
        help="ISO8601 UTC (19:00 МСК = 16:00Z)",
    )
    p.add_argument("--guest-count", type=int, default=2)
    p.add_argument("--guest-name", default="Иван")
    p.add_argument("--guest-phone", default="89169825182")
    p.add_argument("--duration", type=int, default=120)
    p.add_argument("--notes", default="")
    p.add_argument("--table-id", default="", help="Пусто = авто «минимальный свободный стол» как в агенте")
    p.add_argument("--time-zone", default="", help="IANA, например Europe/Moscow (для date= в list_reservations)")
    p.add_argument(
        "--post",
        action="store_true",
        help="Выполнить POST /api/reservations/{org}/{store}/reservations (создаёт бронь в Toka)",
    )
    p.add_argument("--skip-mcp", action="store_true", help="Не вызывать TokaMcpAgent.toka_create_reservation")
    return p.parse_args()


async def _main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    from app.services.toka_client import TokaBackofficeClient, TokaClientError, get_toka_backoffice_client_for_binding
    from app.services.toka_mcp_agent import (
        TokaMcpAgent,
        _load_reservations_for_toka_date,
        _parse_starts_at_utc,
        _pick_smallest_free_table_id,
        _toka_list_date_str,
    )
    from app.storage.database import get_session_maker
    from app.storage.toka_binding_repository import lookup_binding_dto_sync

    args = _parse_args()
    restaurant_ref: Dict[str, Any] = {"name": args.restaurant}

    sm = get_session_maker()
    dto = lookup_binding_dto_sync(sm, restaurant_ref=restaurant_ref)
    if dto is None:
        print("Нет строки toka_restaurant_bindings (в т.ч. default).", file=sys.stderr)
        return 2

    print("=== binding ===")
    print(json.dumps({"id": dto.id, "org_id": dto.org_id, "store_id": dto.store_id, "token_type": dto.token_type}, indent=2))

    if not args.skip_mcp:
        agent = TokaMcpAgent()
        mcp_out = await agent.toka_create_reservation(
            restaurant_ref=restaurant_ref,
            starts_at=args.starts_at,
            guest_count=args.guest_count,
            guest_name=args.guest_name,
            guest_phone=args.guest_phone,
            duration_minutes=args.duration,
            notes=args.notes,
            table_id=(args.table_id or None),
            client_time_zone=(args.time_zone or None) or None,
        )
        print("\n=== TokaMcpAgent.toka_create_reservation (как в приложении) ===")
        print(json.dumps(mcp_out, ensure_ascii=False, indent=2))

    client = await get_toka_backoffice_client_for_binding(dto)
    org_id = dto.org_id.strip()
    st_id = dto.store_id.strip()

    print("\n=== step: GET halls/tables ===")
    try:
        halls_raw = await client.get_halls_and_tables(org_id, st_id)
        print("OK, keys:", list(halls_raw.keys()))
    except TokaClientError as exc:
        print("TokaClientError:", exc)
        return 1

    dur = int(args.duration) if int(args.duration) > 0 else 120
    interval_start = _parse_starts_at_utc(args.starts_at)
    interval_end = interval_start + timedelta(minutes=dur)
    date_str = _toka_list_date_str(args.starts_at, (args.time_zone or None) or None)

    print("\n=== step: GET reservations ===")
    print("toka list date:", date_str, "(interval UTC", interval_start.isoformat(), "–", interval_end.isoformat() + ")")
    try:
        reservations_list = await _load_reservations_for_toka_date(client, org_id, st_id, date_str)
        print("rows:", len(reservations_list))
    except TokaClientError as exc:
        print("TokaClientError:", exc)
        return 1

    table_id_str: Optional[str] = str(args.table_id).strip() if args.table_id else None
    if not table_id_str:
        table_id_str = _pick_smallest_free_table_id(
            halls_raw,
            int(args.guest_count),
            interval_start,
            interval_end,
            reservations_list,
        )

    print("\n=== picked table_id ===")
    print(table_id_str or "(none — агент вернул бы NO_TABLE_AVAILABLE)")

    payload = {
        "table_id": table_id_str,
        "starts_at": args.starts_at,
        "duration_minutes": dur,
        "guest_name": args.guest_name,
        "guest_phone": args.guest_phone,
        "guest_count": int(args.guest_count),
        "notes": args.notes or "",
        "source": "agent",
    }
    path = f"/api/reservations/{org_id}/{st_id}/reservations"

    print("\n=== POST body (как у TokaBackofficeClient.create_reservation) ===")
    print("POST", path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not args.post:
        print("\n(Без --post запрос создания брони в Toka не отправляется.)")
        return 0

    if not table_id_str:
        print("Нет стола для POST — прервались.", file=sys.stderr)
        return 1

    print("\n=== raw POST (status + body) ===")
    await client._ensure_logged_in()  # noqa: SLF001 — диагностический скрипт
    resp = await client._do_authorized("POST", path, json=payload)  # noqa: SLF001
    print("HTTP", resp.status_code)
    print(resp.text)
    if resp.status_code >= 400:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
