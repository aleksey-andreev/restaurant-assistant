#!/usr/bin/env python3
"""
Удалить действующие заказы и брони столиков в Toka для профиля из toka_restaurant_bindings.

Запуск из корня репозитория (подхватывается .env):

  python3 backend/scripts/cleanup_toka_orders_reservations.py --profile default --date-from 2026-05-01

Просмотр заказов без --date-from; брони — только с --date-from:

  python3 backend/scripts/cleanup_toka_orders_reservations.py --profile default
  python3 backend/scripts/cleanup_toka_orders_reservations.py --profile Ипполит --date-from 2026-05-01

Удаление (нужны явные ключи):

  python3 backend/scripts/cleanup_toka_orders_reservations.py --profile default --delete-orders
  python3 backend/scripts/cleanup_toka_orders_reservations.py --profile default --date-from 2026-05-01 --delete-reservations
  python3 backend/scripts/cleanup_toka_orders_reservations.py --profile default --date-from 2026-05-01 --delete-orders --delete-reservations

--date-from (YYYY-MM-DD, UTC) обязателен только для броней; «по» — сегодня.

Токен берётся из строки привязки (token_type refresh или access), как в приложении.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

_OPEN_ORDER_STATUSES = ("active", "cooking", "cooked")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--profile",
        required=True,
        help="restaurant_name в toka_restaurant_bindings (нормализуется: lower, один пробел)",
    )
    p.add_argument(
        "--delete-orders",
        action="store_true",
        help="Ключ: отменить действующие заказы (POST …/cancel)",
    )
    p.add_argument(
        "--delete-reservations",
        action="store_true",
        help="Ключ: удалить действующие брони (DELETE …/reservations/{id})",
    )
    p.add_argument(
        "--date-from",
        default=None,
        metavar="YYYY-MM-DD",
        help="Дата с (UTC) для броней; обязателен при просмотре/удалении reservations; «по» — сегодня",
    )
    return p.parse_args()


def _needs_reservations_scan(args: argparse.Namespace) -> bool:
    dry_run = not (args.delete_orders or args.delete_reservations)
    return bool(args.delete_reservations or dry_run)


def _parse_date_from(value: str) -> date:
    raw = (value or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"ожидается YYYY-MM-DD, получено: {value!r}") from exc


def _calendar_dates_inclusive(date_from: date, date_to: date) -> List[str]:
    if date_from > date_to:
        return []
    out: List[str] = []
    cur = date_from
    while cur <= date_to:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _order_id(row: Dict[str, Any]) -> str:
    for key in ("id", "order_id", "orderId"):
        v = row.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _reservation_id(row: Dict[str, Any]) -> str:
    v = row.get("id")
    if v is not None and str(v).strip():
        return str(v).strip()
    return ""


async def _collect_open_orders(client: Any, store_id: str) -> List[Dict[str, Any]]:
    from app.services.toka_mcp_agent import _order_status_blocks_table

    by_id: Dict[str, Dict[str, Any]] = {}
    for status in _OPEN_ORDER_STATUSES:
        raw = await client.list_orders(store_id, status=status)
        rows = raw.get("items") or raw.get("results") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not _order_status_blocks_table(row.get("status")):
                continue
            oid = _order_id(row)
            if not oid:
                continue
            by_id[oid] = row
    return list(by_id.values())


async def _collect_active_reservations(
    client: Any,
    org_id: str,
    store_id: str,
    date_strings: List[str],
) -> List[Dict[str, Any]]:
    from app.services.toka_mcp_agent import _status_blocks_table

    by_id: Dict[str, Dict[str, Any]] = {}
    for date_str in date_strings:
        raw = await client.list_reservations(org_id, store_id, date_str=date_str)
        rows = raw.get("results") or raw.get("items") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not _status_blocks_table(row.get("status")):
                continue
            rid = _reservation_id(row)
            if not rid:
                continue
            by_id[rid] = row
    return list(by_id.values())


async def _cancel_order(client: Any, store_id: str, order_id: str) -> Dict[str, Any]:
    path = f"/api/orders/{store_id}/{order_id}/cancel"
    return await client.request_json("POST", path)


async def _delete_reservation(
    client: Any, org_id: str, store_id: str, reservation_id: str
) -> Dict[str, Any]:
    path = f"/api/reservations/{org_id}/{store_id}/reservations/{reservation_id}"
    return await client.request_json("DELETE", path)


async def _main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    from app.services.toka_client import TokaClientError, get_toka_backoffice_client_for_binding
    from app.storage.database import get_session_maker
    from app.storage.toka_binding_repository import (
        TokaBindingRepository,
        norm_toka_restaurant_key,
    )

    args = _parse_args()
    scan_reservations = _needs_reservations_scan(args)
    date_from: date | None = None
    date_to: date | None = None
    if scan_reservations:
        if not args.date_from:
            print(
                "Для броней нужен --date-from YYYY-MM-DD (dry-run или --delete-reservations).",
                file=sys.stderr,
            )
            return 2
        date_from = _parse_date_from(args.date_from)
        date_to = datetime.now(timezone.utc).date()
        if date_from > date_to:
            print(
                f"--date-from {date_from.isoformat()} позже сегодня ({date_to.isoformat()} UTC).",
                file=sys.stderr,
            )
            return 2

    profile_key = norm_toka_restaurant_key(args.profile)
    if not profile_key:
        print("Пустой --profile.", file=sys.stderr)
        return 2

    sm = get_session_maker()
    sess = sm()
    try:
        repo = TokaBindingRepository(sess)
        row = repo.get_by_restaurant_name(profile_key)
        if row is None:
            print(
                f"Нет привязки toka_restaurant_bindings.restaurant_name={profile_key!r}.",
                file=sys.stderr,
            )
            return 2
        dto = TokaBindingRepository.row_to_dto(row)
    finally:
        sess.close()

    if dto is None:
        print(f"У привязки {profile_key!r} пустой refresh_token.", file=sys.stderr)
        return 2

    dry_run = not (args.delete_orders or args.delete_reservations)
    org_id = dto.org_id.strip()
    store_id = dto.store_id.strip()

    summary: Dict[str, Any] = {
        "profile": profile_key,
        "binding_id": dto.id,
        "org_id": org_id,
        "store_id": store_id,
        "token_type": dto.token_type,
        "dry_run": dry_run,
        "delete_orders": bool(args.delete_orders),
        "delete_reservations": bool(args.delete_reservations),
    }
    if scan_reservations and date_from is not None and date_to is not None:
        summary["reservations_date_from"] = date_from.isoformat()
        summary["reservations_date_to"] = date_to.isoformat()
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    client = await get_toka_backoffice_client_for_binding(dto)
    errors = 0

    try:
        if args.delete_orders or dry_run:
            orders = await _collect_open_orders(client, store_id)
            print(f"\n=== orders (open: {len(orders)}) ===")
            for row in orders:
                print(
                    json.dumps(
                        {
                            "id": _order_id(row),
                            "status": row.get("status"),
                            "table_id": row.get("table_id"),
                        },
                        ensure_ascii=False,
                    )
                )
            if args.delete_orders and not dry_run:
                for row in orders:
                    oid = _order_id(row)
                    try:
                        await _cancel_order(client, store_id, oid)
                        print(f"cancelled order {oid}")
                    except TokaClientError as exc:
                        errors += 1
                        print(f"FAIL order {oid}: {exc}", file=sys.stderr)

        if scan_reservations and date_from is not None and date_to is not None:
            dates = _calendar_dates_inclusive(date_from, date_to)
            reservations = await _collect_active_reservations(client, org_id, store_id, dates)
            print(
                f"\n=== reservations (active: {len(reservations)}, "
                f"range: {date_from.isoformat()}..{date_to.isoformat()}, days: {len(dates)}) ==="
            )
            for row in reservations:
                print(
                    json.dumps(
                        {
                            "id": _reservation_id(row),
                            "status": row.get("status"),
                            "table_id": row.get("table_id"),
                            "starts_at": row.get("starts_at") or row.get("start_at"),
                        },
                        ensure_ascii=False,
                    )
                )
            if args.delete_reservations and not dry_run:
                for row in reservations:
                    rid = _reservation_id(row)
                    try:
                        await _delete_reservation(client, org_id, store_id, rid)
                        print(f"deleted reservation {rid}")
                    except TokaClientError as exc:
                        errors += 1
                        print(f"FAIL reservation {rid}: {exc}", file=sys.stderr)

        if dry_run:
            print(
                "\nDry-run: ничего не удалено. Добавьте --delete-orders и/или --delete-reservations.",
                file=sys.stderr,
            )
    finally:
        await client.close()

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
