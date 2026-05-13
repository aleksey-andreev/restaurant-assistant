from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .toka_client import (
    TokaBackofficeClient,
    TokaClientError,
    find_table_capacity,
    find_table_title,
    get_toka_backoffice_client_for_binding,
)
from ..storage.database import get_session_maker
from ..storage.toka_binding_repository import lookup_binding_dto_sync

def _ok(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _err(code: str, message: str, retriable: bool, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {
        "code": code,
        "message": message,
        "retriable": retriable,
    }
    if details:
        error["details"] = details
    return {"ok": False, "error": error}


def _normalize_reservation_status(status: Optional[str]) -> str:
    return (status or "").strip().lower().replace("-", "_")


def _status_blocks_table(status: Optional[str]) -> bool:
    """
    Official Toka reservation statuses: confirmed, seated, completed, cancelled, no_show.
    Slot is free for overlap checks unless an *active* booking blocks it; treat
    completed, cancelled, no_show as not occupying the table.
    """
    s = _normalize_reservation_status(status)
    if s in {"completed", "cancelled", "canceled", "no_show", "noshow"}:
        return False
    if s in {"confirmed", "seated"}:
        return True
    # Unknown / empty: conservative — assume the table is still reserved.
    return True


def _parse_starts_at_utc(starts_at: str) -> datetime:
    s = (starts_at or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _overlap_half_open(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    """True if [a0, a1) intersects [b0, b1)."""
    return a0 < b1 and b0 < a1


def _dates_for_reservation_fetch(start: datetime, end: datetime) -> List[str]:
    """UTC calendar dates to query for reservations overlapping [start, end)."""
    if end <= start:
        end = start + timedelta(minutes=120)
    last_calendar = (end - timedelta(microseconds=1)).date()
    cur = start.date()
    days: List[str] = []
    while cur <= last_calendar:
        days.append(cur.isoformat())
        cur = cur + timedelta(days=1)
    return days


def _reservation_interval_utc(row: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
    es = row.get("starts_at")
    ee = row.get("ends_at")
    if not es or not ee:
        return None
    try:
        s = _parse_starts_at_utc(str(es))
        e = _parse_starts_at_utc(str(ee))
        if e <= s:
            return None
        return s, e
    except Exception:
        return None


def _tables_sorted_by_capacity(halls_payload: Dict[str, Any], guest_count: int) -> List[Tuple[str, int]]:
    gc = max(1, int(guest_count))
    cand: List[Tuple[str, int]] = []
    for hall in halls_payload.get("items") or []:
        for table in hall.get("tables") or []:
            cap = table.get("capacity")
            if cap is None:
                continue
            try:
                cap_i = int(cap)
            except Exception:
                continue
            if cap_i < gc:
                continue
            tid = table.get("id")
            if tid is None:
                continue
            cand.append((str(tid), cap_i))
    cand.sort(key=lambda x: x[1])
    return cand


def _table_available_for_interval(
    table_id: str,
    interval_start: datetime,
    interval_end: datetime,
    reservations: List[Dict[str, Any]],
) -> bool:
    for r in reservations:
        if str(r.get("table_id")) != str(table_id):
            continue
        if not _status_blocks_table(r.get("status")):
            continue
        iv = _reservation_interval_utc(r)
        if iv is None:
            continue
        rs, re = iv
        if _overlap_half_open(interval_start, interval_end, rs, re):
            return False
    return True


def _pick_smallest_free_table_id(
    halls_payload: Dict[str, Any],
    guest_count: int,
    interval_start: datetime,
    interval_end: datetime,
    reservations: List[Dict[str, Any]],
) -> Optional[str]:
    for tid, _ in _tables_sorted_by_capacity(halls_payload, guest_count):
        if _table_available_for_interval(tid, interval_start, interval_end, reservations):
            return tid
    return None


def _toka_list_date_str(starts_at: str, client_time_zone: Optional[str]) -> str:
    """
    Calendar date for Toka ``list_reservations(date=…)``: start of the booking interval
    in the client's IANA zone when provided, else UTC calendar date.
    """
    dt_utc = _parse_starts_at_utc(starts_at)
    name = (client_time_zone or "").strip()
    if name:
        try:
            return dt_utc.astimezone(ZoneInfo(name)).date().isoformat()
        except Exception:
            pass
    return dt_utc.date().isoformat()


async def _load_reservations_for_toka_date(
    client: TokaBackofficeClient,
    org_id: str,
    store_id: str,
    toka_date_str: str,
) -> List[Dict[str, Any]]:
    raw = await client.list_reservations(org_id, store_id, date_str=toka_date_str)
    rows = raw.get("results") or raw.get("items") or []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _table_row_title(table: Dict[str, Any], tid: str) -> str:
    for key in ("title", "name", "label"):
        v = table.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return f"Стол {tid}"


def _iter_tables_from_halls(halls_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for hall in halls_payload.get("items") or []:
        for table in hall.get("tables") or []:
            tid = table.get("id")
            if tid is None:
                continue
            cap = table.get("capacity")
            try:
                cap_i = int(cap) if cap is not None else None
            except Exception:
                cap_i = None
            if cap_i is None:
                continue
            ts = str(tid)
            out.append({"id": ts, "capacity": cap_i, "title": _table_row_title(table, ts)})
    return out


def _max_blocking_end_utc(
    table_id: str,
    interval_start: datetime,
    interval_end: datetime,
    reservations: List[Dict[str, Any]],
) -> Optional[datetime]:
    mx: Optional[datetime] = None
    for r in reservations:
        if str(r.get("table_id")) != str(table_id):
            continue
        if not _status_blocks_table(r.get("status")):
            continue
        iv = _reservation_interval_utc(r)
        if iv is None:
            continue
        rs, re = iv
        if _overlap_half_open(interval_start, interval_end, rs, re):
            mx = re if mx is None else max(mx, re)
    return mx


async def _load_reservations_covering_interval(
    client: TokaBackofficeClient,
    org_id: str,
    store_id: str,
    interval_start: datetime,
    interval_end: datetime,
) -> List[Dict[str, Any]]:
    dates = _dates_for_reservation_fetch(interval_start, interval_end)
    seen: set[str] = set()
    merged: List[Dict[str, Any]] = []
    for d in dates:
        raw = await client.list_reservations(org_id, store_id, date_str=d)
        rows = raw.get("results") or raw.get("items") or []
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip()
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            merged.append(r)
    return merged


def _max_table_capacity(halls_payload: Dict[str, Any]) -> int:
    m = 0
    for hall in halls_payload.get("items") or []:
        for table in hall.get("tables") or []:
            cap = table.get("capacity")
            if cap is None:
                continue
            try:
                m = max(m, int(cap))
            except Exception:
                continue
    return m


class TokaMcpAgent:
    """
    MCP-style facade over Toka HTTP calls.

    Each public method loads the row from ``toka_restaurant_bindings`` for the current
    restaurant context (name / org_id / store_id) **in that request**, then obtains the
    HTTP client with that row's ``refresh_token`` — nothing is fixed at application startup.
    """

    async def _binding_dto(
        self,
        *,
        restaurant_ref: Optional[Dict[str, Any]] = None,
        organization_id: Optional[str] = None,
        store_id: Optional[str] = None,
    ):
        sm = get_session_maker()

        def _sync():
            return lookup_binding_dto_sync(
                sm,
                restaurant_ref=restaurant_ref,
                organization_id=organization_id,
                store_id=store_id,
            )

        return await asyncio.to_thread(_sync)

    async def _client_for(
        self,
        *,
        restaurant_ref: Optional[Dict[str, Any]] = None,
        organization_id: Optional[str] = None,
        store_id: Optional[str] = None,
    ) -> TokaBackofficeClient:
        """Resolve binding from DB for this call, then return client with that row's credentials."""
        dto = await self._binding_dto(
            restaurant_ref=restaurant_ref,
            organization_id=organization_id,
            store_id=store_id,
        )
        if dto is None:
            raise TokaClientError(
                "No Toka binding in database: add row restaurant_name='default' "
                "(see init_db seed / toka_restaurant_bindings)."
            )
        return await get_toka_backoffice_client_for_binding(dto)

    async def toka_list_organizations(self) -> Dict[str, Any]:
        try:
            client = await self._client_for(restaurant_ref={"name": ""})
            data = await client.get_my_organizations()
            return _ok({"organizations": list(data.get("items") or [])})
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

    async def toka_list_stores(self, organization_id: str) -> Dict[str, Any]:
        try:
            client = await self._client_for(
                restaurant_ref={},
                organization_id=organization_id,
            )
            data = await client.list_stores(organization_id)
            return _ok({"stores": list(data.get("items") or [])})
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

    async def toka_get_halls_and_tables(self, organization_id: str, store_id: str) -> Dict[str, Any]:
        try:
            dto = await self._binding_dto(
                restaurant_ref={},
                organization_id=organization_id,
                store_id=store_id,
            )
            if dto is None:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka binding in database for this organization/store (or missing default row).",
                    retriable=False,
                )
            client = await get_toka_backoffice_client_for_binding(dto)
            data = await client.get_halls_and_tables(organization_id, store_id)
            return _ok({"halls": list(data.get("items") or []), "raw": data})
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

    async def toka_get_menu_tree(self, organization_id: str, store_id: str) -> Dict[str, Any]:
        try:
            dto = await self._binding_dto(
                restaurant_ref={},
                organization_id=organization_id,
                store_id=store_id,
            )
            if dto is None:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka binding in database for this organization/store (or missing default row).",
                    retriable=False,
                )
            client = await get_toka_backoffice_client_for_binding(dto)
            data = await client.get_menu_tree(organization_id, store_id)
            return _ok({"raw": data})
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

    async def toka_find_capacity(
        self,
        candidate_ref: Dict[str, Any],
        party_size: int,
        starts_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        _ = starts_at
        ps = max(1, int(party_size))
        ref = dict(candidate_ref or {})
        try:
            dto = await self._binding_dto(restaurant_ref=ref)
            if dto is None:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka org/store in toka_restaurant_bindings for this restaurant (or missing default row).",
                    retriable=False,
                )
            org_id = dto.org_id.strip()
            store_id = dto.store_id.strip()
            if not org_id or not store_id:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka org/store in toka_restaurant_bindings for this restaurant (or missing default row).",
                    retriable=False,
                )
            client = await get_toka_backoffice_client_for_binding(dto)
            raw_halls = await client.get_halls_and_tables(org_id, store_id)
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)
        max_capacity = _max_table_capacity(raw_halls)
        return _ok(
            {
                "capacity_verified": max_capacity >= ps,
                "party_size": ps,
                "max_capacity": max_capacity,
                "message": None if max_capacity >= ps else "No table for requested party size",
                "resolved": {"organization_id": org_id, "store_id": store_id},
            }
        )

    async def toka_booking_table_options(
        self,
        restaurant_ref: Dict[str, Any],
        starts_at: str,
        guest_count: int,
        duration_minutes: int = 120,
        client_time_zone: Optional[str] = None,
    ) -> Dict[str, Any]:
        ref = dict(restaurant_ref or {})
        ps = max(1, int(guest_count))
        try:
            dto = await self._binding_dto(restaurant_ref=ref)
            if dto is None:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka org/store in toka_restaurant_bindings for this restaurant (or missing default row).",
                    retriable=False,
                )
            org_id = dto.org_id.strip()
            st_id = dto.store_id.strip()
            if not org_id or not st_id:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka org/store in toka_restaurant_bindings for this restaurant (or missing default row).",
                    retriable=False,
                )
            client = await get_toka_backoffice_client_for_binding(dto)
            halls_raw = await client.get_halls_and_tables(org_id, st_id)
            try:
                dur = int(duration_minutes) if int(duration_minutes) > 0 else 120
            except (TypeError, ValueError):
                dur = 120
            interval_start = _parse_starts_at_utc(starts_at)
            interval_end = interval_start + timedelta(minutes=dur)
            date_str = _toka_list_date_str(starts_at, client_time_zone)
            reservations_list = await _load_reservations_for_toka_date(client, org_id, st_id, date_str)
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

        tables_out: List[Dict[str, Any]] = []
        for row in _iter_tables_from_halls(halls_raw):
            tid = row["id"]
            cap = row["capacity"]
            title = row["title"]
            if cap < ps:
                tables_out.append(
                    {"id": tid, "title": title, "capacity": cap, "status": "too_small", "free_after": None}
                )
                continue
            if _table_available_for_interval(tid, interval_start, interval_end, reservations_list):
                tables_out.append({"id": tid, "title": title, "capacity": cap, "status": "free", "free_after": None})
            else:
                mx = _max_blocking_end_utc(tid, interval_start, interval_end, reservations_list)
                free_after: Optional[str] = None
                if mx is not None:
                    free_after = mx.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                tables_out.append(
                    {"id": tid, "title": title, "capacity": cap, "status": "busy", "free_after": free_after}
                )
        return _ok(
            {
                "tables": tables_out,
                "toka_list_date": date_str,
                "resolved": {"organization_id": org_id, "store_id": st_id},
            }
        )

    async def toka_create_reservation(
        self,
        restaurant_ref: Dict[str, Any],
        starts_at: str,
        guest_count: int,
        guest_name: str,
        guest_phone: str,
        duration_minutes: int = 120,
        notes: str = "",
        idempotency_key: Optional[str] = None,
        table_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        store_id: Optional[str] = None,
        client_time_zone: Optional[str] = None,
    ) -> Dict[str, Any]:
        _ = idempotency_key
        org_kw = (organization_id or "").strip() or None
        st_kw = (store_id or "").strip() or None
        ref = dict(restaurant_ref or {})
        try:
            dto = await self._binding_dto(
                restaurant_ref=ref,
                organization_id=org_kw,
                store_id=st_kw,
            )
            if dto is None:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka org/store in toka_restaurant_bindings for this restaurant (or missing default row).",
                    retriable=False,
                )
            org_id = dto.org_id.strip()
            st_id = dto.store_id.strip()
            if not org_id or not st_id:
                return _err(
                    "RESOLVER_NOT_CONFIGURED",
                    "No Toka org/store in toka_restaurant_bindings for this restaurant (or missing default row).",
                    retriable=False,
                )
            client = await get_toka_backoffice_client_for_binding(dto)
            halls_raw = await client.get_halls_and_tables(org_id, st_id)
            try:
                dur = int(duration_minutes) if int(duration_minutes) > 0 else 120
            except (TypeError, ValueError):
                dur = 120
            interval_start = _parse_starts_at_utc(starts_at)
            interval_end = interval_start + timedelta(minutes=dur)
            date_str = _toka_list_date_str(starts_at, client_time_zone)
            reservations_list = await _load_reservations_for_toka_date(client, org_id, st_id, date_str)
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

        table_id_str: Optional[str] = str(table_id).strip() if table_id else None
        if table_id_str:
            cap = find_table_capacity(halls_raw, table_id_str)
            if cap is None:
                return _err("TABLE_NOT_FOUND", f"Table {table_id_str} not found in store", retriable=False)
            if int(guest_count) > int(cap):
                return _err(
                    "NO_TABLE_AVAILABLE",
                    "guest_count exceeds selected table capacity",
                    retriable=False,
                )
            if not _table_available_for_interval(
                table_id_str,
                interval_start,
                interval_end,
                reservations_list,
            ):
                return _err(
                    "NO_TABLE_AVAILABLE",
                    "Table is not free for the requested time slot",
                    retriable=False,
                )
        else:
            table_id_str = _pick_smallest_free_table_id(
                halls_raw,
                int(guest_count),
                interval_start,
                interval_end,
                reservations_list,
            )
            if not table_id_str:
                return _err(
                    "NO_TABLE_AVAILABLE",
                    "No free table with enough capacity for requested time slot",
                    retriable=False,
                )

        payload = {
            "table_id": table_id_str,
            "starts_at": starts_at,
            "duration_minutes": dur,
            "guest_name": guest_name,
            "guest_phone": guest_phone,
            "guest_count": int(guest_count),
            "notes": notes or "",
            "source": "agent",
        }
        try:
            reservation = await client.create_reservation(org_id, st_id, payload)
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

        reservation_id = reservation.get("id") or reservation.get("reservation_id") or reservation.get("code")
        table_title_str = find_table_title(halls_raw, str(table_id_str))
        return _ok(
            {
                "reservation_id": str(reservation_id) if reservation_id else "",
                "starts_at": starts_at,
                "guest_count": int(guest_count),
                "guest_name": guest_name,
                "guest_phone": guest_phone,
                "table_id": table_id_str,
                "table_title": table_title_str,
                "restaurant_name": str(ref.get("name") or reservation.get("restaurant_name") or ""),
                "restaurant_address": str(ref.get("address") or reservation.get("restaurant_address") or ""),
                "raw": reservation,
                "resolved": {"organization_id": org_id, "store_id": st_id},
            }
        )
