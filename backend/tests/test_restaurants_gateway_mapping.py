from __future__ import annotations

import pathlib
import sys
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.routers import restaurants
from app.services.toka_gateway import TokaGatewayError


class _GatewayNoTable:
    async def create_reservation(self, **kwargs):
        _ = kwargs
        raise TokaGatewayError(code="NO_TABLE_AVAILABLE", message="no table")


class _GatewayTableNotFound:
    async def create_reservation(self, **kwargs):
        _ = kwargs
        raise TokaGatewayError(code="TABLE_NOT_FOUND", message="table missing")


class _GatewaySuccess:
    async def create_reservation(self, **kwargs):
        _ = kwargs
        return {"raw": {"id": "r-1", "status": "ok"}}

    async def list_organizations(self):
        return [{"id": "org1"}]

    async def list_stores(self, organization_id: str):
        _ = organization_id
        return [{"id": "store1", "has_tables": True}]

    async def get_halls_and_tables(self, organization_id: str, store_id: str):
        _ = (organization_id, store_id)
        return {"items": []}


def _make_app(override):
    app = FastAPI()
    app.include_router(restaurants.router, prefix="/api")
    app.dependency_overrides[restaurants.get_toka_gateway_dep] = override
    return app


class RestaurantsGatewayMappingTest(unittest.TestCase):
    def test_no_table_maps_to_400(self) -> None:
        async def _override():
            return _GatewayNoTable()

        client = TestClient(_make_app(_override))
        resp = client.post(
            "/api/stores/store1/reservations?organization_id=org1",
            json={
                "table_id": "t1",
                "starts_at": "2026-04-14T20:00:00+03:00",
                "duration_minutes": 120,
                "guest_name": "Ivan",
                "guest_phone": "+79990000000",
                "guest_count": 4,
                "notes": "",
                "source": "agent",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_table_not_found_maps_to_404(self) -> None:
        async def _override():
            return _GatewayTableNotFound()

        client = TestClient(_make_app(_override))
        resp = client.post(
            "/api/stores/store1/reservations?organization_id=org1",
            json={
                "table_id": "t1",
                "starts_at": "2026-04-14T20:00:00+03:00",
                "duration_minutes": 120,
                "guest_name": "Ivan",
                "guest_phone": "+79990000000",
                "guest_count": 4,
                "notes": "",
                "source": "agent",
            },
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_reservation_success(self) -> None:
        async def _override():
            return _GatewaySuccess()

        client = TestClient(_make_app(_override))
        resp = client.post(
            "/api/stores/store1/reservations?organization_id=org1",
            json={
                "table_id": "t1",
                "starts_at": "2026-04-14T20:00:00+03:00",
                "duration_minutes": 120,
                "guest_name": "Ivan",
                "guest_phone": "+79990000000",
                "guest_count": 4,
                "notes": "",
                "source": "agent",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("id"), "r-1")


if __name__ == "__main__":
    unittest.main()

