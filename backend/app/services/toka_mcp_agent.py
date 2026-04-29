from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from .toka_client import (
    TokaBackofficeClient,
    TokaClientError,
    find_table_capacity,
    get_toka_backoffice_client,
)


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


def _pick_smallest_table_id(halls_payload: Dict[str, Any], guest_count: int) -> Optional[str]:
    chosen_table_id: Optional[str] = None
    chosen_capacity: Optional[int] = None
    for hall in halls_payload.get("items") or []:
        for table in hall.get("tables") or []:
            cap = table.get("capacity")
            if cap is None:
                continue
            try:
                cap_i = int(cap)
            except Exception:
                continue
            if cap_i < guest_count:
                continue
            tid = table.get("id")
            if tid is None:
                continue
            if chosen_capacity is None or cap_i < chosen_capacity:
                chosen_capacity = cap_i
                chosen_table_id = str(tid)
    return chosen_table_id


def _resolve_toka_store_stub(_restaurant_ref: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    # MVP resolver: all external cards map to a single Toka test store from env.
    org = os.environ.get("TOKA_STUB_ORGANIZATION_ID", "").strip()
    store = os.environ.get("TOKA_STUB_STORE_ID", "").strip()
    if not org or not store:
        return None, None
    return org, store


class TokaMcpAgent:
    """Subagent-like MCP tool facade that fully owns Toka HTTP interaction."""

    async def _client(self) -> TokaBackofficeClient:
        return await get_toka_backoffice_client()

    async def toka_list_organizations(self) -> Dict[str, Any]:
        try:
            data = await (await self._client()).get_my_organizations()
            return _ok({"organizations": list(data.get("items") or [])})
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

    async def toka_list_stores(self, organization_id: str) -> Dict[str, Any]:
        try:
            data = await (await self._client()).list_stores(organization_id)
            return _ok({"stores": list(data.get("items") or [])})
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

    async def toka_get_halls_and_tables(self, organization_id: str, store_id: str) -> Dict[str, Any]:
        try:
            data = await (await self._client()).get_halls_and_tables(organization_id, store_id)
            return _ok({"halls": list(data.get("items") or []), "raw": data})
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
        org_id, store_id = _resolve_toka_store_stub(candidate_ref)
        if org_id is None or store_id is None:
            return _err(
                "RESOLVER_NOT_CONFIGURED",
                "Set TOKA_STUB_ORGANIZATION_ID and TOKA_STUB_STORE_ID",
                retriable=False,
            )
        halls_res = await self.toka_get_halls_and_tables(org_id, store_id)
        if not halls_res.get("ok"):
            return halls_res
        raw_halls = halls_res["data"]["raw"]
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
    ) -> Dict[str, Any]:
        _ = idempotency_key
        org_id = (organization_id or "").strip() or None
        st_id = (store_id or "").strip() or None
        if not org_id or not st_id:
            org_id, st_id = _resolve_toka_store_stub(restaurant_ref)
        if not org_id or not st_id:
            return _err(
                "RESOLVER_NOT_CONFIGURED",
                "Set TOKA_STUB_ORGANIZATION_ID and TOKA_STUB_STORE_ID",
                retriable=False,
            )

        halls_res = await self.toka_get_halls_and_tables(org_id, st_id)
        if not halls_res.get("ok"):
            return halls_res
        halls_raw = halls_res["data"]["raw"]

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
        else:
            table_id_str = _pick_smallest_table_id(halls_raw, int(guest_count))
            if not table_id_str:
                return _err(
                    "NO_TABLE_AVAILABLE",
                    "No table with enough capacity for guest_count",
                    retriable=False,
                )

        payload = {
            "table_id": table_id_str,
            "starts_at": starts_at,
            "duration_minutes": int(duration_minutes) if int(duration_minutes) > 0 else 120,
            "guest_name": guest_name,
            "guest_phone": guest_phone,
            "guest_count": int(guest_count),
            "notes": notes or "",
            "source": "agent",
        }
        try:
            reservation = await (await self._client()).create_reservation(org_id, st_id, payload)
        except TokaClientError as exc:
            return _err("TOKA_API_ERROR", str(exc), retriable=True)
        except Exception as exc:
            return _err("TOKA_UNKNOWN_ERROR", str(exc), retriable=True)

        reservation_id = reservation.get("id") or reservation.get("reservation_id") or reservation.get("code")
        return _ok(
            {
                "reservation_id": str(reservation_id) if reservation_id else "",
                "starts_at": starts_at,
                "guest_count": int(guest_count),
                "guest_name": guest_name,
                "guest_phone": guest_phone,
                "table_id": table_id_str,
                "restaurant_name": str(restaurant_ref.get("name") or reservation.get("restaurant_name") or ""),
                "restaurant_address": str(restaurant_ref.get("address") or reservation.get("restaurant_address") or ""),
                "raw": reservation,
                "resolved": {"organization_id": org_id, "store_id": st_id},
            }
        )

