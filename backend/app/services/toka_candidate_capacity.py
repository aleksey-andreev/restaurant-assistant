"""
Toka capacity gate for Afisha candidates: stub resolver + halls/tables check.

Until Afisha→Toka search exists, TOKA_STUB_ORGANIZATION_ID and TOKA_STUB_STORE_ID
must point at the test account's org and store (see .cursor/rules).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .toka_gateway import TokaGatewayError, get_toka_gateway

# Demote formal_score when capacity could not be verified (stub/env/API).
TOKA_UNVERIFIED_SCORE_FACTOR = 0.85

_MSG_NO_ENV = (
    "Не удалось подтвердить стол: задайте TOKA_STUB_ORGANIZATION_ID и TOKA_STUB_STORE_ID "
    "(org и store тестового аккаунта Toka)."
)
_MSG_API = "Не удалось подтвердить наличие стола в системе бронирования (ошибка Toka)."
_MSG_TOO_SMALL = "В тестовой точке Toka нет стола на заявленное число гостей."


def resolve_toka_store_stub(name: str, address: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Placeholder for future Afisha→Toka resolver. Accepts name/address for stable call sites.

    Returns (organization_id, store_id) from environment or (None, None).
    """
    _ = (name, address)
    org = os.environ.get("TOKA_STUB_ORGANIZATION_ID", "").strip()
    store = os.environ.get("TOKA_STUB_STORE_ID", "").strip()
    if not org or not store:
        return None, None
    return org, store


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
    org_id, store_id = resolve_toka_store_stub(name0, addr_s)

    if org_id is None or store_id is None:
        return _annotate_all(_MSG_NO_ENV, None), errors

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
