"""
Toka capacity gate for Afisha candidates: resolver reads org/store + tokens from table
toka_restaurant_bindings (fallback row restaurant_name=default).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .toka_gateway import TokaGatewayError, get_toka_gateway

# Demote formal_score when capacity could not be verified (stub/env/API).
TOKA_UNVERIFIED_SCORE_FACTOR = 0.85

_MSG_NO_DB = (
    "Не удалось подтвердить стол: нет записи Toka в БД для этого ресторана "
    "(или отсутствует строка default в toka_restaurant_bindings)."
)
_MSG_API = "Не удалось подтвердить наличие стола в системе бронирования (ошибка Toka)."
_MSG_TOO_SMALL = "В тестовой точке Toka нет стола на заявленное число гостей."


async def resolve_toka_store_stub_async(name: str, address: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve org_id/store_id from toka_restaurant_bindings by restaurant name, else default row."""
    _ = address

    def _sync():
        from ..storage.database import get_session_maker
        from ..storage.toka_binding_repository import TokaBindingRepository, norm_toka_restaurant_key

        sm = get_session_maker()
        sess = sm()
        try:
            repo = TokaBindingRepository(sess)
            nk = norm_toka_restaurant_key(name)
            row = repo.resolve_binding(restaurant_name_key=nk)
            if row is None:
                return None, None
            o = row.org_id.strip()
            s = row.store_id.strip()
            if not o or not s:
                return None, None
            return o, s
        finally:
            sess.close()

    return await asyncio.to_thread(_sync)


async def apply_toka_capacity_gate(
    candidates: List[Dict[str, Any]],
    party_size: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Single test store: one halls fetch. Either all candidates annotated as verified,
    all unverified (env/API), or list cleared when max capacity < party_size.
    """
    errors: List[str] = []
    ps = max(1, int(party_size)) if party_size >= 1 else 1

    def _annotate_all(
        msg: Optional[str],
        verified: Optional[bool],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in candidates:
            cc = dict(c)
            cc["toka_capacity_verified"] = verified
            cc["toka_capacity_message"] = msg
            out.append(cc)
        return out

    if not candidates:
        return [], errors

    name0 = str(candidates[0].get("name") or "")
    addr0 = candidates[0].get("address")
    addr_s = str(addr0).strip() if addr0 else None
    org_id, store_id = await resolve_toka_store_stub_async(name0, addr_s)

    if org_id is None or store_id is None:
        return _annotate_all(_MSG_NO_DB, None), errors

    try:
        gateway = await get_toka_gateway()
        capacity = await gateway.find_capacity(
            candidate_ref={"name": name0, "address": addr_s},
            party_size=ps,
        )
    except TokaGatewayError as exc:
        errors.append(str(exc))
        return _annotate_all(_MSG_API, None), errors
    except Exception as exc:
        errors.append(str(exc))
        return _annotate_all(_MSG_API, None), errors

    max_cap = int(capacity.get("max_capacity") or 0)
    if max_cap < ps:
        return [], errors + [_MSG_TOO_SMALL]

    out: List[Dict[str, Any]] = []
    for c in candidates:
        cc = dict(c)
        cc["toka_capacity_verified"] = True
        cc["toka_capacity_message"] = None
        out.append(cc)
    return out, errors


def apply_toka_unverified_score_penalty(scored_candidates: List[Dict[str, Any]]) -> None:
    """In-place: multiply formal_score if Toka capacity was not verified."""
    for c in scored_candidates:
        if c.get("toka_capacity_verified") is True:
            continue
        if not c.get("toka_capacity_message"):
            continue
        fs = float(c.get("formal_score") or 0.0)
        c["formal_score"] = max(0.0, min(1.0, fs * TOKA_UNVERIFIED_SCORE_FACTOR))
