from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from .toka_mcp_agent import TokaMcpAgent


class TokaGatewayError(Exception):
    def __init__(self, code: str, message: str, retriable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable


class TokaGateway:
    """Adapter used by app services; delegates all Toka work to MCP subagent."""

    def __init__(self, mcp_agent: TokaMcpAgent) -> None:
        self._agent = mcp_agent

    @staticmethod
    def _unwrap(result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("ok") is True:
            data = result.get("data")
            return data if isinstance(data, dict) else {}
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        raise TokaGatewayError(
            code=str(error.get("code") or "TOKA_GATEWAY_ERROR"),
            message=str(error.get("message") or "Toka gateway call failed"),
            retriable=bool(error.get("retriable")),
        )

    async def list_organizations(self) -> List[Dict[str, Any]]:
        return list(self._unwrap(await self._agent.toka_list_organizations()).get("organizations") or [])

    async def list_stores(self, organization_id: str) -> List[Dict[str, Any]]:
        return list(self._unwrap(await self._agent.toka_list_stores(organization_id)).get("stores") or [])

    async def get_halls_and_tables(self, organization_id: str, store_id: str) -> Dict[str, Any]:
        data = self._unwrap(await self._agent.toka_get_halls_and_tables(organization_id, store_id))
        return data.get("raw") if isinstance(data.get("raw"), dict) else {"items": list(data.get("halls") or [])}

    async def get_menu_tree(self, organization_id: str, store_id: str) -> Dict[str, Any]:
        data = self._unwrap(await self._agent.toka_get_menu_tree(organization_id, store_id))
        raw = data.get("raw")
        return raw if isinstance(raw, dict) else {}

    async def find_capacity(
        self,
        candidate_ref: Dict[str, Any],
        party_size: int,
        starts_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._unwrap(
            await self._agent.toka_find_capacity(
                candidate_ref=candidate_ref,
                party_size=party_size,
                starts_at=starts_at,
            )
        )

    async def list_booking_table_options(
        self,
        restaurant_ref: Dict[str, Any],
        starts_at: str,
        guest_count: int,
        duration_minutes: int = 120,
        client_time_zone: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._unwrap(
            await self._agent.toka_booking_table_options(
                restaurant_ref=restaurant_ref,
                starts_at=starts_at,
                guest_count=guest_count,
                duration_minutes=duration_minutes,
                client_time_zone=client_time_zone,
            )
        )

    async def create_reservation(
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
        return self._unwrap(
            await self._agent.toka_create_reservation(
                restaurant_ref=restaurant_ref,
                starts_at=starts_at,
                guest_count=guest_count,
                guest_name=guest_name,
                guest_phone=guest_phone,
                duration_minutes=duration_minutes,
                notes=notes,
                idempotency_key=idempotency_key,
                table_id=table_id,
                organization_id=organization_id,
                store_id=store_id,
                client_time_zone=client_time_zone,
            )
        )


_toka_gateway_singleton: Optional[TokaGateway] = None
_toka_gateway_lock = asyncio.Lock()


async def get_toka_gateway() -> TokaGateway:
    global _toka_gateway_singleton
    if _toka_gateway_singleton is not None:
        return _toka_gateway_singleton
    async with _toka_gateway_lock:
        if _toka_gateway_singleton is not None:
            return _toka_gateway_singleton
        _toka_gateway_singleton = TokaGateway(mcp_agent=TokaMcpAgent())
        return _toka_gateway_singleton

