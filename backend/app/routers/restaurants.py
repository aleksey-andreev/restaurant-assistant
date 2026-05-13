from __future__ import annotations

from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services.toka_gateway import TokaGateway, TokaGatewayError, get_toka_gateway

router = APIRouter(tags=["restaurants"])


def _raise_toka_gateway_http(exc: TokaGatewayError) -> None:
    code = getattr(exc, "code", "")
    if code == "NO_TABLE_AVAILABLE":
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if code == "TABLE_NOT_FOUND":
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise HTTPException(status_code=502, detail=str(exc)) from exc


async def get_toka_gateway_dep() -> TokaGateway:
    try:
        return await get_toka_gateway()
    except TokaGatewayError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


TokaGatewayDep = Annotated[TokaGateway, Depends(get_toka_gateway_dep)]


@router.get("/search/organizations")
async def search_organizations(
    gateway: TokaGatewayDep,
    query: str = "",
) -> Dict[str, Any]:
    """
    Stub search: ignores query and returns the first organization from /organizations/my.
    """
    _ = query
    try:
        items = await gateway.list_organizations()
    except TokaGatewayError as exc:
        _raise_toka_gateway_http(exc)
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
    gateway: TokaGatewayDep,
    organization_id: str,
    query: str = "",
) -> Dict[str, Any]:
    """
    Stub search: ignores query; returns first store with has_tables=True, else first store.
    """
    _ = query
    try:
        items = await gateway.list_stores(organization_id)
    except TokaGatewayError as exc:
        _raise_toka_gateway_http(exc)
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
    gateway: TokaGatewayDep,
    store_id: str,
    organization_id: str,
) -> Dict[str, Any]:
    try:
        return await gateway.get_halls_and_tables(organization_id, store_id)
    except TokaGatewayError as exc:
        _raise_toka_gateway_http(exc)


@router.get("/menus/{organization_id}/stores/{store_id}/menus/tree")
async def menu_tree(
    gateway: TokaGatewayDep,
    organization_id: str,
    store_id: str,
) -> Dict[str, Any]:
    """
    Прокси к Toka: ``GET /api/menus/{org}/stores/{store}/menus/tree``.
    Учётные данные берутся из строки ``toka_restaurant_bindings`` для пары org/store (или default).
    """
    try:
        return await gateway.get_menu_tree(organization_id, store_id)
    except TokaGatewayError as exc:
        _raise_toka_gateway_http(exc)


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
    gateway: TokaGatewayDep,
    store_id: str,
    organization_id: str,
    payload: CreateReservationRequest,
) -> Dict[str, Any]:
    try:
        result = await gateway.create_reservation(
            restaurant_ref={},
            starts_at=payload.starts_at,
            guest_count=payload.guest_count,
            guest_name=payload.guest_name,
            guest_phone=payload.guest_phone,
            duration_minutes=payload.duration_minutes,
            notes=payload.notes,
            table_id=payload.table_id,
            organization_id=organization_id,
            store_id=store_id,
        )
        return dict(result.get("raw") or result)
    except TokaGatewayError as exc:
        _raise_toka_gateway_http(exc)
