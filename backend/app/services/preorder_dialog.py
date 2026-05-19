from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..storage.state_repository import StateRepository
from .llm import LLMClientRegistry
from .receipt_booking_snapshot import build_receipt_booking_snapshot
from .preorder_service import (
    build_toka_order_payload,
    compact_menu_for_llm,
    format_preorder_summary_ru,
    is_short_affirmative_reply,
    is_short_decline_reply,
    iter_menu_positions,
    lines_from_menu_item_ids,
    menu_tree_has_positions,
    parse_llm_menu_pick_json,
    preorder_cart_total,
    preorder_menu_pick_response_format,
    wants_llm_pick_phrase,
    wants_open_menu_phrase,
)

logger = logging.getLogger(__name__)

PREORDER_LLM_NODE = "preorder_menu_pick"
PREORDER_PIPELINE_STAGE_LLM_PICK = "preorder_llm_pick"
_PREORDER_LLM_RAW_MAX_CHARS = 50_000
_PREORDER_ACTIVE = frozenset({"offer", "mode_choice", "browsing", "summary"})


@dataclass(frozen=True)
class PreorderLlmPickOutcome:
    picks: List[Dict[str, Any]]
    raw: Optional[str]
    ok: bool
    error: Optional[str]
    model: Optional[str]


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user" and m.get("content") is not None:
            return str(m.get("content") or "").strip()
    return ""


async def _persist(
    state_repository: StateRepository,
    session_id: str,
    messages: List[Dict[str, Any]],
    reply: str,
    current_node: str,
    context_patch: Dict[str, Any],
) -> Dict[str, Any]:
    st = await state_repository.get_state_for_session(session_id)
    full = dict(st.context or {})
    full.update(context_patch)
    await state_repository.update_current_node_and_context(session_id, current_node, full)
    await state_repository.append_history(session_id, messages, reply)
    updated = await state_repository.get_state_for_session(session_id)
    return {"reply": reply, "session_id": session_id, "state": updated.to_dict()}


async def _llm_pick_items(
    llm_registry: LLMClientRegistry,
    menu_compact: List[Dict[str, Any]],
    user_instruction: str,
    *,
    current_cart_json: str = "[]",
    guest_count: int = 1,
) -> PreorderLlmPickOutcome:
    llm_client, system_prompt, node_params = llm_registry.get_node_or_default(PREORDER_LLM_NODE)
    model = str(node_params.get("model") or "") or None
    cat = json.dumps(menu_compact, ensure_ascii=False)
    prompt = (
        "Подбери позиции из каталога под пожелания пользователя.\n"
        f"Гостей за столом: {max(1, guest_count)}.\n"
        f"Текущая корзина (можно заменить полностью): {current_cart_json}\n"
        f"Пожелания: {user_instruction}\n"
        f"Каталог (id, title, price, section, nutrition, portion, ingredients, …):\n{cat}\n"
    )
    sys_content = (system_prompt or "").strip() or (
        "Подбери блюда из каталога. Ответ — только JSON по схеме structured output (ключ items)."
    )
    messages = [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": prompt},
    ]
    raw: Optional[str] = None
    last_exc: Optional[Exception] = None
    for fmt_params in (
        preorder_menu_pick_response_format(strict=True),
        preorder_menu_pick_response_format(strict=False),
        {"response_format": {"type": "json_object"}},
    ):
        try:
            raw = await llm_client.chat(
                messages=messages,
                **{**node_params, **fmt_params},
            )
            break
        except Exception as exc:
            last_exc = exc
            logger.debug(
                "preorder LLM pick response_format retry after: %s",
                exc,
            )
    if raw is None:
        logger.warning("preorder LLM pick failed: %s", last_exc)
        return PreorderLlmPickOutcome(
            picks=[],
            raw=None,
            ok=False,
            error=str(last_exc)[:500] if last_exc else "LLM call failed",
            model=model,
        )
    raw_s = raw or ""
    picks = parse_llm_menu_pick_json(raw_s)
    return PreorderLlmPickOutcome(
        picks=picks,
        raw=raw_s,
        ok=True,
        error=None,
        model=model,
    )


async def _record_preorder_llm_pick_event(
    state_repository: StateRepository,
    session_id: str,
    *,
    source: str,
    preferences: str,
    guest_count: int,
    menu_positions_count: int,
    outcome: PreorderLlmPickOutcome,
    cart_lines: List[Dict[str, Any]],
) -> None:
    """Append preorder LLM pick trace to pipeline_events (same body shape as graph trace)."""
    raw = outcome.raw or ""
    if len(raw) > _PREORDER_LLM_RAW_MAX_CHARS:
        raw = raw[:_PREORDER_LLM_RAW_MAX_CHARS] + "…[truncated]"
    event = {
        "stage": PREORDER_PIPELINE_STAGE_LLM_PICK,
        "ts": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "source": source,
            "preferences": (preferences or "")[:4000],
            "guest_count": max(1, guest_count),
            "menu_positions_count": menu_positions_count,
            "llm_node": PREORDER_LLM_NODE,
            "model": outcome.model,
            "ok": outcome.ok,
            "error": outcome.error,
            "raw": raw,
            "picks": outcome.picks,
            "picks_count": len(outcome.picks),
            "cart_lines_count": len(cart_lines),
            "cart_lines": [
                {
                    "menu_item_id": ln.get("menu_item_id"),
                    "quantity": ln.get("quantity"),
                    "title": ln.get("title"),
                    "price": ln.get("price"),
                }
                for ln in cart_lines
                if isinstance(ln, dict)
            ],
        },
    }
    await state_repository.append_pipeline_events(
        session_id,
        str(uuid.uuid4()),
        [event],
    )


def _refinement_heuristic(text: str) -> bool:
    t = (text or "").strip().lower()
    if len(t) < 8:
        return False
    keys = (
        "удали",
        "замени",
        "убери",
        "добавь",
        "ещё",
        "еще",
        "без ",
        "вегет",
        "постн",
        "аллерг",
        "остр",
        "детск",
        "птиц",
        "рыб",
        "мяс",
    )
    return any(k in t for k in keys)


async def try_handle_preorder_dialog(
    *,
    session_id: str,
    messages: List[Dict[str, Any]],
    client_action: Optional[Dict[str, Any]],
    ctx: Dict[str, Any],
    state_repository: StateRepository,
    llm_registry: LLMClientRegistry,
) -> Optional[Dict[str, Any]]:
    phase_raw = ctx.get("preorder_phase")
    phase = str(phase_raw).strip() if phase_raw is not None else ""
    if phase not in _PREORDER_ACTIVE:
        return None

    ca = client_action or {}
    ca_type = str(ca.get("type") or "")

    if ca_type in {"submit_booking", "select_booking_candidate", "confirm_search_plan"}:
        return None

    last_user = _last_user_text(messages)

    org_id = str(ctx.get("preorder_organization_id") or "").strip()
    store_id = str(ctx.get("preorder_store_id") or "").strip()
    if not org_id or not store_id:
        return await _persist(
            state_repository,
            session_id,
            messages,
            "Предзаказ недоступен: не заданы данные точки Toka.",
            "preorder_error",
            {"preorder_phase": "declined", "preorder_menu_available": False},
        )

    from .toka_gateway import TokaGatewayError, get_toka_gateway

    async def _menu() -> Dict[str, Any]:
        gw = await get_toka_gateway()
        return await gw.get_menu_tree(org_id, store_id)

    req = ctx.get("booking_requirements") if isinstance(ctx.get("booking_requirements"), dict) else {}
    resv = ctx.get("reservation_result") if isinstance(ctx.get("reservation_result"), dict) else {}
    guest_name = str(resv.get("guest_name") or req.get("guest_name") or "").strip()
    guest_phone = str(resv.get("guest_phone") or req.get("guest_phone") or "").strip()
    try:
        guest_count = int(ctx.get("preorder_guest_count") or resv.get("guest_count") or req.get("guest_count") or 1)
    except (TypeError, ValueError):
        guest_count = 1
    guest_count = max(1, min(1000, guest_count))
    table_id = str(ctx.get("preorder_table_id") or resv.get("table_id") or req.get("table_id") or "").strip()

    # --- offer ---
    if phase == "offer":
        if ca_type == "preorder_decline_offer" or is_short_decline_reply(last_user):
            return await _persist(
                state_repository,
                session_id,
                messages,
                "Хорошо, предзаказ оформлять не будем. Приятного визита!",
                "preorder_declined",
                {"preorder_phase": "declined", "preorder_menu_available": bool(ctx.get("preorder_menu_available"))},
            )
        if ca_type == "confirm_preorder_offer" or is_short_affirmative_reply(last_user):
            reply = (
                "Отлично. Как удобнее?\n"
                "— Ручной выбор блюд: кнопка «Выберу сам из меню» ниже откроет карточку меню.\n"
                "— Или опишите предпочтения в сообщении — мы подберём позиции автоматически "
                "(например: «вегетарианское, не острое, до 2000 ₽»)."
            )
            return await _persist(
                state_repository,
                session_id,
                messages,
                reply,
                "preorder_mode_choice",
                {"preorder_phase": "mode_choice"},
            )
        return None

    # --- mode_choice ---
    if phase == "mode_choice":
        if ca_type == "preorder_choose_manual" or wants_open_menu_phrase(last_user):
            return await _persist(
                state_repository,
                session_id,
                messages,
                "Выберите позиции в карточке меню ниже.",
                "preorder_browsing",
                {"preorder_phase": "browsing"},
            )
        if ca_type == "preorder_llm_pick":
            prefs = str(ca.get("preferences_text") or "").strip()
            if not prefs:
                prefs = last_user
        elif wants_llm_pick_phrase(last_user) or (last_user and len(last_user) >= 5):
            prefs = last_user
        else:
            prefs = ""

        if prefs:
            try:
                tree = await _menu()
            except (TokaGatewayError, Exception) as exc:
                logger.warning("preorder menu fetch failed: %s", exc)
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "Не удалось загрузить меню. Попробуйте кнопку «Выберу сам из меню» или повторите позже.",
                    "preorder_mode_choice",
                    {"preorder_phase": "mode_choice"},
                )
            if not menu_tree_has_positions(tree):
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "Меню сейчас пустое. Оформите заказ через кнопку «Выберу сам из меню», когда позиции появятся.",
                    "preorder_mode_choice",
                    {"preorder_phase": "mode_choice"},
                )
            compact = compact_menu_for_llm(tree)
            outcome = await _llm_pick_items(llm_registry, compact, prefs, guest_count=guest_count)
            lines = lines_from_menu_item_ids(tree, outcome.picks)
            await _record_preorder_llm_pick_event(
                state_repository,
                session_id,
                source="mode_choice",
                preferences=prefs,
                guest_count=guest_count,
                menu_positions_count=len(compact),
                outcome=outcome,
                cart_lines=lines,
            )
            if not lines:
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "Не удалось подобрать блюда по описанию. Уточните пожелания или выберите из меню вручную.",
                    "preorder_mode_choice",
                    {"preorder_phase": "mode_choice"},
                )
            return await _persist(
                state_repository,
                session_id,
                messages,
                "Подобрали варианты заказа. Проверьте и при необходимости измените выбор в карточке меню ниже.",
                "preorder_browsing",
                {"preorder_phase": "browsing", "preorder_cart_lines": lines},
            )

        return None

    # --- browsing ---
    if phase == "browsing":
        if ca_type == "preorder_submit_cart":
            raw_lines = ca.get("lines")
            if not isinstance(raw_lines, list):
                raw_lines = []
            try:
                tree = await _menu()
            except (TokaGatewayError, Exception) as exc:
                logger.warning("preorder menu fetch failed: %s", exc)
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "Не удалось загрузить меню для проверки заказа.",
                    "preorder_browsing",
                    {"preorder_phase": "browsing"},
                )
            by_mid = {r["menu_item_id"]: r for r in iter_menu_positions(tree)}
            norm: List[Dict[str, Any]] = []
            for row in raw_lines:
                if not isinstance(row, dict):
                    continue
                mid = str(row.get("menu_item_id") or "").strip()
                if not mid or mid not in by_mid:
                    continue
                try:
                    q = int(row.get("quantity") or 1)
                except (TypeError, ValueError):
                    q = 1
                q = max(1, min(99, q))
                base = by_mid[mid]
                unit = float(base.get("price") or 0.0)
                norm.append(
                    {
                        "menu_item_id": mid,
                        "quantity": q,
                        "title": str(base.get("title") or "—"),
                        "price": unit,
                        "line_total": round(unit * q, 2),
                        "section": str(base.get("section") or ""),
                    }
                )
            if not norm:
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "В заказе нет позиций из меню. Отметьте блюда и нажмите «Сформировать».",
                    "preorder_browsing",
                    {"preorder_phase": "browsing"},
                )
            summary = format_preorder_summary_ru(norm)
            reply = f"{summary}\n\nПодтвердите заказ кнопкой «Подтвердить» или вернитесь к правкам — «Внести изменения»."
            return await _persist(
                state_repository,
                session_id,
                messages,
                reply,
                "preorder_summary",
                {"preorder_phase": "summary", "preorder_cart_lines": norm},
            )

        cart_lines = ctx.get("preorder_cart_lines") if isinstance(ctx.get("preorder_cart_lines"), list) else []
        if cart_lines and last_user and _refinement_heuristic(last_user):
            try:
                tree = await _menu()
            except (TokaGatewayError, Exception) as exc:
                logger.warning("preorder menu fetch failed: %s", exc)
                return None
            compact = compact_menu_for_llm(tree)
            current_json = json.dumps(
                [{"menu_item_id": x.get("menu_item_id"), "quantity": x.get("quantity")} for x in cart_lines if isinstance(x, dict)],
                ensure_ascii=False,
            )
            refine_instruction = f"Учти текущую корзину и пожелание пользователя. {last_user}"
            outcome = await _llm_pick_items(
                llm_registry,
                compact,
                refine_instruction,
                current_cart_json=current_json,
                guest_count=guest_count,
            )
            lines = lines_from_menu_item_ids(tree, outcome.picks)
            await _record_preorder_llm_pick_event(
                state_repository,
                session_id,
                source="browsing_refine",
                preferences=refine_instruction,
                guest_count=guest_count,
                menu_positions_count=len(compact),
                outcome=outcome,
                cart_lines=lines,
            )
            if lines:
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "Обновили подбор по вашему запросу. Проверьте карточку меню.",
                    "preorder_browsing",
                    {"preorder_phase": "browsing", "preorder_cart_lines": lines},
                )
        return None

    # --- summary ---
    if phase == "summary":
        if ca_type == "preorder_amend":
            lines = ctx.get("preorder_cart_lines") if isinstance(ctx.get("preorder_cart_lines"), list) else []
            return await _persist(
                state_repository,
                session_id,
                messages,
                "Вернитесь к выбору в карточке меню, измените позиции и снова нажмите «Сформировать».",
                "preorder_browsing",
                {"preorder_phase": "browsing", "preorder_cart_lines": lines},
            )
        if ca_type == "preorder_confirm_order" or is_short_affirmative_reply(last_user):
            lines = ctx.get("preorder_cart_lines") if isinstance(ctx.get("preorder_cart_lines"), list) else []
            if not lines:
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "Корзина пуста — нечего отправлять.",
                    "preorder_summary",
                    {"preorder_phase": "summary"},
                )
            payload = build_toka_order_payload(
                client_name=guest_name,
                client_phone=guest_phone,
                guest_count=guest_count,
                table_id=table_id,
                menu_lines=lines,
            )
            try:
                gw = await get_toka_gateway()
                out = await gw.create_order(org_id, store_id, payload)
            except TokaGatewayError as exc:
                logger.warning("Toka create_order failed: %s", exc)
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    f"Не удалось отправить заказ в ресторан: {exc}",
                    "preorder_summary",
                    {"preorder_phase": "summary"},
                )
            except Exception as exc:
                logger.exception("create_order unexpected")
                return await _persist(
                    state_repository,
                    session_id,
                    messages,
                    "Техническая ошибка при отправке заказа. Попробуйте позже.",
                    "preorder_summary",
                    {"preorder_phase": "summary"},
                )
            norm_lines = [x for x in lines if isinstance(x, dict)]
            total = preorder_cart_total(norm_lines)
            raw = out.get("raw") if isinstance(out, dict) else out
            _ = raw
            ok_msg = (
                f"Предзаказ на сумму {total:.0f} ₽ успешно оформлен.\n\n"
                "Сохранить данные о брони и заказе в PDF? "
                "Напишите «да», «ок» — или нажмите «Сохранить»."
            )
            return await _persist(
                state_repository,
                session_id,
                messages,
                ok_msg,
                "preorder_done",
                {
                    "preorder_phase": "done",
                    "preorder_cart_lines": [],
                    "preorder_receipt_lines": norm_lines,
                    "preorder_receipt_total": total,
                    "receipt_booking_snapshot": build_receipt_booking_snapshot(ctx),
                    "save_receipt_offered": True,
                    "save_receipt_done": False,
                    "preorder_order_result": out if isinstance(out, dict) else {"raw": out},
                },
            )
        return None

    return None
