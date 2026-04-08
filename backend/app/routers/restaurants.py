from __future__ import annotations

from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services.toka_client import (
    TokaBackofficeClient,
    TokaClientError,
    find_table_capacity,
    get_toka_backoffice_client,
)

router = APIRouter(tags=["restaurants"])


async def get_toka_client_dep() -> TokaBackofficeClient:
    try:
        return await get_toka_backoffice_client()
    except TokaClientError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


TokaClientDep = Annotated[TokaBackofficeClient, Depends(get_toka_client_dep)]


@router.get("/search/organizations")
async def search_organizations(
    client: TokaClientDep,
    query: str = "",
) -> Dict[str, Any]:
    """
    Stub search: ignores query and returns the first organization from /organizations/my.
    """
    _ = query
    try:
        data = await client.get_my_organizations()
    except TokaClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = list(data.get("items") or [])
    if not items:
        return {
            "items": [],
            "total": 0,
            "page": 1,
            "size": 0,
            "additional_items": [],
        }
    return {
        "items": [items[0]],
        "total": 1,
        "page": 1,
        "size": 1,
        "additional_items": [],
    }


@router.get("/search/stores")
async def search_stores(
    client: TokaClientDep,
    organization_id: str,
    query: str = "",
) -> Dict[str, Any]:
    """
    Stub search: ignores query; returns first store with has_tables=True, else first store.
    """
    _ = query
    try:
        data = await client.list_stores(organization_id)
    except TokaClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = list(data.get("items") or [])
    if not items:
        return {
            "items": [],
            "total": 0,
            "page": 1,
            "size": 0,
            "additional_items": [],
        }
    chosen: Optional[Dict[str, Any]] = None
    for store in items:
        if store.get("has_tables") is True:
            chosen = store
            break
    if chosen is None:
        chosen = items[0]
    return {
        "items": [chosen],
        "total": 1,
        "page": 1,
        "size": 1,
        "additional_items": [],
    }


@router.get("/stores/{store_id}/halls-tables")
async def halls_and_tables(
    client: TokaClientDep,
    store_id: str,
    organization_id: str,
) -> Dict[str, Any]:
    try:
        return await client.get_halls_and_tables(organization_id, store_id)
    except TokaClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class CreateReservationRequest(BaseModel):
    table_id: str
    starts_at: str = Field(..., description="ISO-8601 start time, e.g. 2026-04-01T19:14:32.987Z")
    duration_minutes: int = 120
    guest_name: str
    guest_phone: str
    guest_count: int = Field(..., ge=1)
    notes: str = ""
    source: str = "agent"


@router.post("/stores/{store_id}/reservations")
async def create_reservation(
    client: TokaClientDep,
    store_id: str,
    organization_id: str,
    payload: CreateReservationRequest,
) -> Dict[str, Any]:
    try:
        halls = await client.get_halls_and_tables(organization_id, store_id)
    except TokaClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    cap = find_table_capacity(halls, payload.table_id)
    if cap is None:
        raise HTTPException(
            status_code=404,
            detail=f"Table {payload.table_id} not found in this store",
        )
    if payload.guest_count > cap:
        raise HTTPException(
            status_code=400,
            detail=(
                f"guest_count ({payload.guest_count}) exceeds table capacity ({cap}). "
                "Choose a larger table or reduce party size."
            ),
        )

    body = payload.model_dump()
    try:
        return await client.create_reservation(organization_id, store_id, body)
    except TokaClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
