from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx


class TokaClientError(Exception):
    """Raised when Toka API returns an error or auth chain fails."""


def _toka_base_url() -> str:
    return (
        os.environ.get("TOKA_REST_URL")
        or os.environ.get("TOKA_BACKOFFICE_BASE_URL")
        or "https://backoffice.toka.rest"
    ).rstrip("/")


class TokaBackofficeClient:
    """
    Async client for Toka Backoffice API with login, refresh-on-401, and real routes.

    Authorization: Toka examples use raw JWT in `authorization` header (no Bearer prefix).
    """

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        self._auth_lock = asyncio.Lock()

    def _json_headers_public(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _auth_headers(self) -> Dict[str, str]:
        if not self._access_token:
            raise TokaClientError("Not authenticated")
        return {
            "Accept": "application/json",
            "authorization": self._access_token,
        }

    async def close(self) -> None:
        await self._client.aclose()

    async def _login_unlocked(self) -> None:
        resp = await self._client.post(
            "/api/users/login",
            json={"username": self._username, "password": self._password},
            headers=self._json_headers_public(),
        )
        if resp.status_code >= 400:
            raise TokaClientError(
                f"Toka login failed: {resp.status_code} {resp.text[:500]}"
            )
        data = resp.json()
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        if not self._access_token or not self._refresh_token:
            raise TokaClientError("Toka login response missing tokens")

    async def _refresh_unlocked(self) -> None:
        if not self._refresh_token:
            await self._login_unlocked()
            return
        resp = await self._client.post(
            "/api/users/refresh-token",
            json={"refresh_token": self._refresh_token},
            headers=self._json_headers_public(),
        )
        if resp.status_code >= 400:
            await self._login_unlocked()
            return
        data = resp.json()
        self._access_token = data.get("access_token") or self._access_token
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        if not self._access_token or not self._refresh_token:
            await self._login_unlocked()

    async def _ensure_logged_in(self) -> None:
        if self._access_token and self._refresh_token:
            return
        async with self._auth_lock:
            if self._access_token and self._refresh_token:
                return
            await self._login_unlocked()

    async def _do_authorized(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> httpx.Response:
        await self._ensure_logged_in()
        headers = self._auth_headers()
        return await self._client.request(
            method, path, headers=headers, json=json, params=params
        )

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> Dict[str, Any]:
        """
        Perform request; on 401 refresh tokens once, then login once, then fail.
        """
        resp = await self._do_authorized(method, path, json=json, params=params)
        if resp.status_code == 401:
            async with self._auth_lock:
                await self._refresh_unlocked()
            resp = await self._do_authorized(method, path, json=json, params=params)
        if resp.status_code == 401:
            async with self._auth_lock:
                await self._login_unlocked()
            resp = await self._do_authorized(method, path, json=json, params=params)
        if resp.status_code >= 400:
            raise TokaClientError(
                f"Toka API error {resp.status_code} on {path}: {resp.text[:1000]}"
            )
        if resp.content:
            return resp.json()
        return {}

    async def get_my_organizations(self) -> Dict[str, Any]:
        return await self.request_json("GET", "/api/units/organizations/my")

    async def list_stores(self, organization_id: str) -> Dict[str, Any]:
        return await self.request_json(
            "GET", f"/api/units/stores/{organization_id}"
        )

    async def get_halls_and_tables(
        self, organization_id: str, store_id: str
    ) -> Dict[str, Any]:
        return await self.request_json(
            "GET",
            f"/api/units/stores/halls/store/{organization_id}/{store_id}",
        )

    async def create_reservation(
        self,
        organization_id: str,
        store_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self.request_json(
            "POST",
            f"/api/reservations/{organization_id}/{store_id}/reservations",
            json=payload,
        )


_toka_singleton: Optional[TokaBackofficeClient] = None
_toka_singleton_lock = asyncio.Lock()


async def get_toka_backoffice_client() -> TokaBackofficeClient:
    """Shared client per process (tokens kept in memory)."""
    global _toka_singleton
    if _toka_singleton is not None:
        return _toka_singleton
    async with _toka_singleton_lock:
        if _toka_singleton is not None:
            return _toka_singleton
        username = os.environ.get("TOKA_USERNAME", "").strip()
        password = os.environ.get("TOKA_PASSWORD", "").strip()
        if not username or not password:
            raise TokaClientError(
                "Set TOKA_USERNAME and TOKA_PASSWORD in environment (.env)"
            )
        _toka_singleton = TokaBackofficeClient(
            base_url=_toka_base_url(),
            username=username,
            password=password,
        )
        return _toka_singleton


def find_table_capacity(
    halls_payload: Dict[str, Any], table_id: str
) -> Optional[int]:
    """Walk halls response and return capacity for table_id, if present."""
    for hall in halls_payload.get("items") or []:
        for table in hall.get("tables") or []:
            if str(table.get("id")) == str(table_id):
                cap = table.get("capacity")
                if cap is None:
                    return None
                return int(cap)
    return None


