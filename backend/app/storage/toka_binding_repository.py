from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import TokaRestaurantBinding


def norm_toka_restaurant_key(name: Optional[str]) -> str:
    """Lowercase single-spaced key for lookup (matches catalog restaurant names loosely)."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


@dataclass(frozen=True)
class TokaBindingDTO:
    id: int
    org_id: str
    store_id: str
    refresh_token: str
    token_type: str


class TokaBindingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_org_id(self, org_id: str) -> Optional[TokaRestaurantBinding]:
        """Any row for this organization (same Toka account across stores)."""
        if not org_id.strip():
            return None
        return self._session.scalar(
            select(TokaRestaurantBinding)
            .where(TokaRestaurantBinding.org_id == org_id.strip())
            .limit(1)
        )

    def get_by_org_and_store(self, org_id: str, store_id: str) -> Optional[TokaRestaurantBinding]:
        if not org_id.strip() or not store_id.strip():
            return None
        return self._session.scalar(
            select(TokaRestaurantBinding).where(
                TokaRestaurantBinding.org_id == org_id.strip(),
                TokaRestaurantBinding.store_id == store_id.strip(),
            )
        )

    def get_by_restaurant_name(self, normalized_name: str) -> Optional[TokaRestaurantBinding]:
        if not normalized_name:
            return None
        return self._session.scalar(
            select(TokaRestaurantBinding).where(
                TokaRestaurantBinding.restaurant_name == normalized_name
            )
        )

    def get_default(self) -> Optional[TokaRestaurantBinding]:
        return self._session.scalar(
            select(TokaRestaurantBinding).where(TokaRestaurantBinding.restaurant_name == "default")
        )

    def resolve_binding(
        self,
        *,
        restaurant_name_key: str,
        organization_id: Optional[str] = None,
        store_id: Optional[str] = None,
    ) -> Optional[TokaRestaurantBinding]:
        """
        Prefer org_id+store_id when both given (REST calls with explicit ids).
        Else match org_id alone if given (e.g. list stores for one organization).
        Else match by normalized restaurant name.
        Else row restaurant_name='default'.
        """
        o = (organization_id or "").strip()
        s = (store_id or "").strip()
        if o and s:
            hit = self.get_by_org_and_store(o, s)
            if hit is not None:
                return hit
        if o and not s:
            hit = self.get_by_org_id(o)
            if hit is not None:
                return hit
        nk = norm_toka_restaurant_key(restaurant_name_key)
        if nk:
            hit = self.get_by_restaurant_name(nk)
            if hit is not None:
                return hit
        return self.get_default()

    @staticmethod
    def row_to_dto(row: TokaRestaurantBinding) -> Optional[TokaBindingDTO]:
        rt = str(row.refresh_token or "").strip()
        if not rt:
            return None
        token_type = str(getattr(row, "token_type", "refresh") or "refresh").strip().lower()
        if token_type not in {"refresh", "access"}:
            token_type = "refresh" if str(row.restaurant_name or "").strip().lower() == "default" else "access"
        return TokaBindingDTO(
            id=int(row.id),
            org_id=str(row.org_id),
            store_id=str(row.store_id),
            refresh_token=rt,
            token_type=token_type,
        )


def lookup_binding_dto_sync(
    session_maker: sessionmaker,
    *,
    restaurant_ref: Optional[dict],
    organization_id: Optional[str] = None,
    store_id: Optional[str] = None,
) -> Optional[TokaBindingDTO]:
    """Sync helper for asyncio.to_thread — closes session before returning."""
    ref = restaurant_ref or {}
    name_key = norm_toka_restaurant_key(str(ref.get("name") or ""))
    sess = session_maker()
    try:
        repo = TokaBindingRepository(sess)
        row = repo.resolve_binding(
            restaurant_name_key=name_key,
            organization_id=organization_id,
            store_id=store_id,
        )
        if row is None:
            return None
        return TokaBindingRepository.row_to_dto(row)
    finally:
        sess.close()
