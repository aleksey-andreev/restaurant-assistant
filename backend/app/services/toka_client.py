from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Dict, Optional, Tuple

import httpx

from ..storage.toka_binding_repository import TokaBindingDTO

logger = logging.getLogger(__name__)


class TokaClientError(Exception):
    """Raised when Toka API returns an error or auth chain fails."""


def _toka_base_url() -> str:
    return (
        os.environ.get("TOKA_REST_URL")
        or os.environ.get("TOKA_BACKOFFICE_BASE_URL")
        or "https://backoffice.toka.rest"
    ).rstrip("/")


def _reservation_create_timeout_sec() -> float:
    """POST create reservation can exceed default client timeout; clamp 30..300 s."""
    raw = os.environ.get("TOKA_RESERVATION_CREATE_TIMEOUT_SEC", "120")
    try:
        return max(30.0, min(float(raw), 300.0))
    except (TypeError, ValueError):
        return 120.0


def _log_toka_token_response(operation: str, resp: httpx.Response) -> None:
    """Полное тело ответа Toka при выдаче/обновлении токенов (в лог сервера)."""
    logger.info(
        "Toka %s: HTTP %s, full response body: %s",
        operation,
        resp.status_code,
        resp.text,
    )


def sync_toka_login(username: str, password: str, base_url: Optional[str] = None) -> Tuple[str, str]:
    """
    Synchronous login for DB seed / migrations only.
    Returns (access_token, refresh_token).
    """
    bu = (base_url or _toka_base_url()).rstrip("/")
    with httpx.Client(base_url=bu, timeout=30.0) as client:
        resp = client.post(
            "/api/users/login",
            json={"username": username, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        _log_toka_token_response("POST /api/users/login (sync seed)", resp)
        if resp.status_code >= 400:
            raise TokaClientError(
                f"Toka login failed: {resp.status_code} {resp.text[:500]}"
            )
        data = resp.json()
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            raise TokaClientError("Toka login response missing tokens")
        return str(access), str(refresh)


ReloadRefreshFn = Optional[Callable[[], Awaitable[Optional[str]]]]


class TokaBackofficeClient:
    """
    Async client for Toka Backoffice API with login, refresh-on-401, and real routes.

    Authorization: Toka examples use raw JWT in `authorization` header (no Bearer prefix).

    DB-backed bindings store token in ``refresh_token`` column and explicit ``token_type``:
    - refresh: obtain access via /api/users/refresh-token
    - access: use token as-is in authorization header
    On 401, optional reload_refresh_from_db() loads latest token from PostgreSQL before retrying.
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_type: str = "refresh",
        binding_id: Optional[int] = None,
        reload_refresh_from_db: ReloadRefreshFn = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = (username or "").strip()
        self._password = (password or "").strip()
        self._access_token = (access_token or "").strip() or None
        self._refresh_token = (refresh_token or "").strip() or None
        tt = (token_type or "refresh").strip().lower()
        if tt not in {"refresh", "access"}:
            raise TokaClientError(f"Unsupported token_type: {token_type}")
        self._token_type = tt
        if self._token_type == "access" and self._refresh_token and not self._access_token:
            self._access_token = self._refresh_token
            self._refresh_token = None
        self._binding_id = binding_id
        self._reload_refresh_from_db = reload_refresh_from_db
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

    def sync_refresh_from_dto(self, dto: TokaBindingDTO) -> None:
        """
        Apply refresh_token from a fresh DB read for this request.
        If the refresh rotated in the database since the last call, drop access so the next
        request re-runs /api/users/refresh-token.
        """
        if self._binding_id is not None and int(dto.id) != int(self._binding_id):
            raise TokaClientError("Cached Toka client binding id mismatch")
        new_token = dto.refresh_token.strip()
        if not new_token:
            raise TokaClientError("Empty token in TokaBindingDTO")
        token_type = (dto.token_type or "refresh").strip().lower()
        if token_type not in {"refresh", "access"}:
            raise TokaClientError(f"Unsupported token_type in TokaBindingDTO: {dto.token_type}")
        self._token_type = token_type
        if self._token_type == "access":
            if new_token != (self._access_token or "").strip():
                self._access_token = new_token
            self._refresh_token = None
            return
        if new_token != (self._refresh_token or "").strip():
            self._refresh_token = new_token
            self._access_token = None

    async def _persist_refresh_token_to_db(self) -> None:
        """Write current refresh_token to toka_restaurant_bindings after Toka rotates it."""
        if self._token_type != "refresh":
            return
        if self._binding_id is None or not self._refresh_token:
            return
        bid = self._binding_id
        token = self._refresh_token

        def sync() -> None:
            from ..storage.database import get_session_maker
            from ..storage.models import TokaRestaurantBinding

            sm = get_session_maker()
            sess = sm()
            try:
                row = sess.get(TokaRestaurantBinding, bid)
                if row is not None:
                    row.refresh_token = token
                    sess.commit()
            finally:
                sess.close()

        await asyncio.to_thread(sync)

    async def _login_unlocked(self) -> None:
        resp = await self._client.post(
            "/api/users/login",
            json={"username": self._username, "password": self._password},
            headers=self._json_headers_public(),
        )
        _log_toka_token_response("POST /api/users/login", resp)
        if resp.status_code >= 400:
            raise TokaClientError(
                f"Toka login failed: {resp.status_code} {resp.text[:500]}"
            )
        data = resp.json()
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        if not self._access_token or not self._refresh_token:
            raise TokaClientError("Toka login response missing tokens")
        await self._persist_refresh_token_to_db()

    async def _refresh_unlocked(self) -> None:
        if not self._refresh_token:
            if self._username and self._password:
                await self._login_unlocked()
                return
            raise TokaClientError(
                "Toka: missing refresh_token (configure toka_restaurant_bindings.refresh_token)"
            )
        resp = await self._client.post(
            "/api/users/refresh-token",
            json={"refresh_token": self._refresh_token},
            headers=self._json_headers_public(),
        )
        _log_toka_token_response("POST /api/users/refresh-token", resp)
        if resp.status_code >= 400:
            if self._username and self._password:
                await self._login_unlocked()
                return
            raise TokaClientError(
                "Toka refresh failed; update refresh_token for this binding in toka_restaurant_bindings"
            )
        data = resp.json()
        self._access_token = data.get("access_token") or self._access_token
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        if not self._access_token or not self._refresh_token:
            if self._username and self._password:
                await self._login_unlocked()
                return
            raise TokaClientError(
                "Toka refresh response incomplete; update refresh_token in toka_restaurant_bindings"
            )
        await self._persist_refresh_token_to_db()

    async def _recover_from_401(self) -> None:
        """Invalidate access; reload refresh from DB if configured; obtain new access."""
        if self._token_type == "access":
            if self._reload_refresh_from_db:
                new_t = await self._reload_refresh_from_db()
                if new_t:
                    self._access_token = new_t.strip()
                    return
            raise TokaClientError(
                "Toka unauthorized with access token; update token in toka_restaurant_bindings"
            )
        self._access_token = None
        if self._reload_refresh_from_db:
            new_r = await self._reload_refresh_from_db()
            if new_r:
                self._refresh_token = new_r.strip()
        if self._refresh_token:
            await self._refresh_unlocked()
        elif self._username and self._password:
            await self._login_unlocked()
        else:
            raise TokaClientError(
                "Toka unauthorized: cannot recover without refresh_token or username/password"
            )

    async def _ensure_logged_in(self) -> None:
        if self._access_token:
            return
        async with self._auth_lock:
            if self._access_token:
                return
            if self._token_type == "access":
                if self._access_token:
                    return
                raise TokaClientError(
                    "Not authenticated: set access token in toka_restaurant_bindings for token_type=access"
                )
            if self._refresh_token:
                await self._refresh_unlocked()
            elif self._username and self._password:
                await self._login_unlocked()
            else:
                raise TokaClientError(
                    "Not authenticated: set refresh_token or TOKA_USERNAME/TOKA_PASSWORD"
                )

    async def _do_authorized(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        await self._ensure_logged_in()
        headers = self._auth_headers()
        if timeout is not None:
            return await self._client.request(
                method, path, headers=headers, json=json, params=params, timeout=timeout
            )
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
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Perform request; on 401 reload refresh from DB (if configured), refresh tokens, retry twice.
        """
        resp = await self._do_authorized(method, path, json=json, params=params, timeout=timeout)
        if resp.status_code == 401:
            async with self._auth_lock:
                await self._recover_from_401()
            resp = await self._do_authorized(method, path, json=json, params=params, timeout=timeout)
        if resp.status_code == 401:
            async with self._auth_lock:
                await self._recover_from_401()
            resp = await self._do_authorized(method, path, json=json, params=params, timeout=timeout)
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

    async def get_menu_tree(
        self, organization_id: str, store_id: str
    ) -> Dict[str, Any]:
        return await self.request_json(
            "GET",
            f"/api/menus/{organization_id}/stores/{store_id}/menus/tree",
        )

    async def create_order(self, store_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = (store_id or "").strip()
        if not sid:
            raise TokaClientError("create_order: empty store_id")
        return await self.request_json("POST", f"/api/orders/{sid}", json=payload)

    async def list_reservations(
        self,
        organization_id: str,
        store_id: str,
        *,
        date_str: str,
    ) -> Dict[str, Any]:
        """Day-scoped reservation list (Toka requires query param ``date``)."""
        return await self.request_json(
            "GET",
            f"/api/reservations/{organization_id}/{store_id}/reservations",
            params={"date": date_str},
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
            timeout=_reservation_create_timeout_sec(),
        )


_toka_clients_by_binding_id: Dict[int, TokaBackofficeClient] = {}
_toka_clients_lock = asyncio.Lock()


async def get_toka_backoffice_client_for_binding(dto: TokaBindingDTO) -> TokaBackofficeClient:
    """
    Returns a cached httpx-backed client per binding id.

    ``dto`` must come from ``lookup_binding_dto_sync`` for the **current** MCP/tool request so
    token + token_type match the row for the resolved restaurant (not values frozen at startup).
    Each call applies ``dto`` to the cached client via ``sync_refresh_from_dto``.
    """
    from ..storage.database import get_session_maker
    from ..storage.models import TokaRestaurantBinding

    async with _toka_clients_lock:
        cached = _toka_clients_by_binding_id.get(dto.id)
        if cached is not None:
            cached.sync_refresh_from_dto(dto)
            return cached
        bid = dto.id

        async def reload_refresh_from_db() -> Optional[str]:
            def sync_load() -> Optional[str]:
                sm = get_session_maker()
                sess = sm()
                try:
                    row = sess.get(TokaRestaurantBinding, bid)
                    if row is None:
                        return None
                    rt = str(row.refresh_token or "").strip()
                    return rt or None
                finally:
                    sess.close()

            return await asyncio.to_thread(sync_load)

        # So refresh failures (expired/revoked DB token) can re-login via /api/users/login
        # without manual DB edits, same creds as init_db seed (TOKA_USERNAME / TOKA_PASSWORD).
        toka_user = os.environ.get("TOKA_USERNAME", "").strip()
        toka_pwd = os.environ.get("TOKA_PASSWORD", "").strip()

        client = TokaBackofficeClient(
            _toka_base_url(),
            toka_user,
            toka_pwd,
            refresh_token=dto.refresh_token,
            token_type=dto.token_type,
            binding_id=bid,
            reload_refresh_from_db=reload_refresh_from_db,
        )
        _toka_clients_by_binding_id[dto.id] = client
        return client


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


def find_table_title(halls_payload: Dict[str, Any], table_id: str) -> str:
    """Human-readable table label from halls payload (Toka card fields vary)."""
    tid = str(table_id)
    for hall in halls_payload.get("items") or []:
        for table in hall.get("tables") or []:
            if str(table.get("id")) != tid:
                continue
            for key in ("title", "name", "label"):
                v = table.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return f"Стол {tid}"
    return f"Стол {tid}"
