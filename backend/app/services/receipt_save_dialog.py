from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..storage.state_repository import StateRepository
from .preorder_service import is_short_affirmative_reply

_SAVE_INTENT_RE = re.compile(
    r"^(сохрани|сохранить|скачай|скачать|выгрузи|выгрузить)(\b|[.!?]|$)",
    re.IGNORECASE,
)


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content") is not None:
            return str(m.get("content") or "").strip()
    return ""


def is_save_receipt_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 64:
        return False
    return bool(_SAVE_INTENT_RE.match(t))


async def try_handle_receipt_save(
    *,
    session_id: str,
    messages: List[Dict[str, Any]],
    client_action: Optional[Dict[str, Any]],
    ctx: Dict[str, Any],
    state_repository: StateRepository,
) -> Optional[Dict[str, Any]]:
    phase = str(ctx.get("preorder_phase") or "").strip()
    if phase != "done":
        return None
    if not ctx.get("save_receipt_offered"):
        return None
    if ctx.get("save_receipt_done"):
        return None

    ca = client_action or {}
    ca_type = str(ca.get("type") or "")
    last_user = _last_user_text(messages)

    triggered = ca_type == "save_receipt" or is_save_receipt_intent(last_user) or is_short_affirmative_reply(
        last_user
    )
    if not triggered:
        return None

    st = await state_repository.get_state_for_session(session_id)
    full = dict(st.context or {})
    full["save_receipt_done"] = True
    await state_repository.update_current_node_and_context(session_id, "receipt_saved", full)
    await state_repository.append_history(session_id, messages, "")
    updated = await state_repository.get_state_for_session(session_id)
    return {"reply": "", "session_id": session_id, "state": updated.to_dict()}
