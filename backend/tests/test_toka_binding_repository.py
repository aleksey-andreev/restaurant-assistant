from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.models import Base, TokaRestaurantBinding
from app.storage.toka_binding_repository import TokaBindingRepository


class TokaBindingRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_resolve_default_fallback(self) -> None:
        s = self.Session()
        try:
            s.add(
                TokaRestaurantBinding(
                    restaurant_name="default",
                    org_id="o-def",
                    store_id="s-def",
                    refresh_token="rt",
                )
            )
            s.commit()
            repo = TokaBindingRepository(s)
            row = repo.resolve_binding(restaurant_name_key="любой ресторан")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.org_id, "o-def")
            self.assertEqual(row.store_id, "s-def")
        finally:
            s.close()

    def test_resolve_by_name_over_default(self) -> None:
        s = self.Session()
        try:
            s.add(
                TokaRestaurantBinding(
                    restaurant_name="default",
                    org_id="o-def",
                    store_id="s-def",
                    refresh_token="rt",
                )
            )
            s.add(
                TokaRestaurantBinding(
                    restaurant_name="кафе орёл",
                    org_id="o-other",
                    store_id="s-other",
                    refresh_token="rt2",
                )
            )
            s.commit()
            repo = TokaBindingRepository(s)
            row = repo.resolve_binding(restaurant_name_key="кафе орёл")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.org_id, "o-other")

            row2 = repo.resolve_binding(restaurant_name_key="нет в таблице")
            self.assertIsNotNone(row2)
            assert row2 is not None
            self.assertEqual(row2.restaurant_name, "default")
        finally:
            s.close()

    def test_resolve_org_then_store(self) -> None:
        s = self.Session()
        try:
            s.add(
                TokaRestaurantBinding(
                    restaurant_name="default",
                    org_id="o-def",
                    store_id="s-def",
                    refresh_token="rt",
                )
            )
            s.add(
                TokaRestaurantBinding(
                    restaurant_name="точка б",
                    org_id="o-b",
                    store_id="s-b",
                    refresh_token="rtb",
                )
            )
            s.commit()
            repo = TokaBindingRepository(s)
            row = repo.resolve_binding(restaurant_name_key="", organization_id="o-b", store_id="s-b")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.store_id, "s-b")
        finally:
            s.close()

    def test_resolve_org_only(self) -> None:
        s = self.Session()
        try:
            s.add(
                TokaRestaurantBinding(
                    restaurant_name="default",
                    org_id="o-def",
                    store_id="s-def",
                    refresh_token="rt",
                )
            )
            s.add(
                TokaRestaurantBinding(
                    restaurant_name="org-only",
                    org_id="o-x",
                    store_id="s-x",
                    refresh_token="rtx",
                )
            )
            s.commit()
            repo = TokaBindingRepository(s)
            row = repo.resolve_binding(restaurant_name_key="", organization_id="o-x")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.org_id, "o-x")
        finally:
            s.close()

    def test_row_to_dto_uses_access_for_non_default_on_invalid_type(self) -> None:
        s = self.Session()
        try:
            row = TokaRestaurantBinding(
                restaurant_name="эвкалипт",
                org_id="o-e",
                store_id="s-e",
                refresh_token="token",
                token_type="legacy",
            )
            s.add(row)
            s.commit()
            dto = TokaBindingRepository.row_to_dto(row)
            self.assertIsNotNone(dto)
            assert dto is not None
            self.assertEqual(dto.token_type, "access")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
