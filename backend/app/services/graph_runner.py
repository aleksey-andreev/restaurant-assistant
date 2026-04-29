from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

from ..services.afisha_city_slug import resolve_afisha_city_slug
from ..services.llm import LLMClientRegistry
from ..storage.session_store import SessionStore
from ..storage.state_repository import StateRepository


def fingerprint_search_plan(req: Dict[str, Any]) -> str:
    """Stable fingerprint for search-relevant requirement fields (confirm / invalidate)."""
    city = (req.get("city") or "").strip().lower() if isinstance(req.get("city"), str) else ""
    ps = req.get("party_size")
    party: Optional[int] = None
    if isinstance(ps, (int, float)) and not isinstance(ps, bool) and int(ps) >= 1:
        party = int(ps)
    br = req.get("budget_range") or {}
    mn = mx = None
    if isinstance(br, dict):
        try:
            mn = float(br.get("min"))
            mx = float(br.get("max"))
        except (TypeError, ValueError):
            pass
    loc = req.get("location")
    loc_key: Any = None
    if isinstance(loc, dict):
        loc_key = (loc.get("type"), loc.get("value"))
    cw = sorted(
        str(x).strip().lower()
        for x in (req.get("cuisine_wanted") or [])
        if isinstance(x, str) and x.strip()
    )
    ca = sorted(
        str(x).strip().lower()
        for x in (req.get("cuisine_avoid") or [])
        if isinstance(x, str) and x.strip()
    )
    payload = {
        "city": city,
        "party": party,
        "budget": (mn, mx) if mn is not None and mx is not None else None,
        "loc": loc_key,
        "cw": cw,
        "ca": ca,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def format_search_plan_summary(req: Dict[str, Any]) -> str:
    """Human-readable plan for user confirmation (no occasion; cuisine default)."""
    city = req.get("city") if isinstance(req.get("city"), str) else ""
    city = city.strip() or "—"
    ps = req.get("party_size")
    party_s = f"{int(ps)}" if isinstance(ps, (int, float)) and not isinstance(ps, bool) and int(ps) >= 1 else "—"

    br = req.get("budget_range") or {}
    mn = mx = None
    if isinstance(br, dict):
        try:
            mn = float(br.get("min"))
            mx = float(br.get("max"))
        except (TypeError, ValueError):
            pass
    if mn is not None and mx is not None:
        budget_s = f"от {_fmt_money(mn)} до {_fmt_money(mx)} ₽ на человека"
    else:
        budget_s = "—"

    loc = req.get("location")
    if isinstance(loc, dict) and loc.get("type") in {"metro", "area"}:
        lv = loc.get("value")
        if isinstance(lv, str) and lv.strip():
            loc_s = f"{loc.get('type')}: {lv.strip()}"
        else:
            loc_s = "весь город (без привязки к метро/району)"
    elif isinstance(loc, dict) and loc.get("type") == "none":
        loc_s = "весь город (без привязки к метро/району)"
    else:
        loc_s = "—"

    wanted = [str(x).strip() for x in (req.get("cuisine_wanted") or []) if isinstance(x, str) and str(x).strip()]
    avoided = [str(x).strip() for x in (req.get("cuisine_avoid") or []) if isinstance(x, str) and str(x).strip()]
    if wanted:
        cuisine_s = ", ".join(wanted[:6]) + ("…" if len(wanted) > 6 else "")
    elif avoided:
        cuisine_s = f"без ограничений по типу; исключить: {', '.join(avoided[:4])}"
    else:
        cuisine_s = "без ограничений по кухне"

    lines = [
        "Параметры поиска (проверьте и подтвердите):",
        f"- Город: {city}",
        f"- Гостей: {party_s}",
        f"- Бюджет: {budget_s}",
        f"- Локация: {loc_s}",
        f"- Кухня: {cuisine_s}",
        "",
        "Если всё верно — нажмите «Подтвердить поиск» под чатом. "
        "Чтобы изменить параметры — напишите уточнение в чате.",
    ]
    return "\n".join(lines)


def _fmt_money(v: float) -> str:
    if abs(v - round(v)) < 0.005:
        return str(int(round(v)))
    return f"{v:.0f}"


class RecState(TypedDict, total=False):
    session_id: str
    current_node: str
    # user-facing
    reply: str
    # requirements / user intent
    recommendation_requirements: Dict[str, Any]
    requirements_complete: bool
    missing_fields: List[str]
    # yandex
    yandex_queries: List[str]
    yandex_urls: List[str]
    # candidates
    candidates: List[Dict[str, Any]]
    # ranking
    scored_candidates: List[Dict[str, Any]]
    min_score: float
    above_threshold_count: int
    shortlist: List[Dict[str, Any]]
    # reviews
    reviews_aspects: List[Dict[str, Any]]
    # final
    final_recommendations: List[Dict[str, Any]]
    recommendations: List[Dict[str, Any]]
    # fallback
    relax_attempts: int
    service_errors: List[str]
    # booking via Toka
    booking_pending: bool
    booking_selected_candidate: Dict[str, Any]
    booking_requirements: Dict[str, Any]
    booking_complete: bool
    booking_missing_fields: List[str]
    reservation_result: Dict[str, Any]
    booking_errors: List[str]
    # plan → act: user must confirm extracted search params before Yandex/Afisha
    search_plan_confirmed: bool
    search_plan_fingerprint: Optional[str]
    repeated_missing_fields: List[str]
    requirements_prompt_count: int
    # analytics: append-only events persisted in graph_state.context
    pipeline_trace: List[Dict[str, Any]]


@dataclass
class GraphRunner:
    """
    Thin façade over the LangGraph/LangChain graph.

    For now this is a simplified placeholder that:
    - loads session & graph state from PostgreSQL via repositories
    - calls an LLM client with a system prompt from configuration
    - returns updated reply and state
    """

    session_store: SessionStore
    state_repository: StateRepository
    llm_registry: LLMClientRegistry

    async def run_dialog(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str],
        client_action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        session = await self.session_store.get_or_create_session(session_id)
        session_id = session.session_id

        graph_state = await self.state_repository.get_state_for_session(session_id)
        ctx = graph_state.context or {}

        booking_selected_override: Optional[Dict[str, Any]] = None
        if client_action and client_action.get("type") == "select_booking_candidate":
            idx = client_action.get("index")
            candidates = (
                ctx.get("final_recommendations")
                or ctx.get("recommendations")
                or ctx.get("shortlist")
                or []
            )
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                c = candidates[idx]
                if isinstance(c, dict):
                    booking_selected_override = c
            if booking_selected_override is None:
                reply = "Не удалось выбрать ресторан: обновите список и попробуйте ещё раз."
                await self.state_repository.append_history(
                    session_id=session_id,
                    messages=messages,
                    reply=reply,
                )
                updated_state = await self.state_repository.get_state_for_session(session_id)
                return {"reply": reply, "session_id": session_id, "state": updated_state.to_dict()}

            next_ctx: Dict[str, Any] = dict(ctx)
            next_ctx["booking_pending"] = True
            next_ctx["booking_selected_candidate"] = booking_selected_override
            next_ctx["booking_complete"] = False
            next_ctx["booking_missing_fields"] = ["starts_at", "guest_count", "guest_name", "guest_phone"]
            next_ctx["booking_errors"] = []
            if not isinstance(next_ctx.get("booking_requirements"), dict):
                next_ctx["booking_requirements"] = {}
            reply = "Ресторан выбран. Заполните форму бронирования ниже и нажмите «Отправить заявку»."
            await self.state_repository.append_history(
                session_id=session_id,
                messages=messages,
                reply=reply,
            )
            await self.state_repository.update_current_node_and_context(
                session_id=session_id,
                current_node="select_booking_candidate",
                context=next_ctx,
            )
            updated_state = await self.state_repository.get_state_for_session(session_id)
            return {"reply": reply, "session_id": session_id, "state": updated_state.to_dict()}

        form_booking_payload: Optional[Dict[str, Any]] = None
        if client_action and client_action.get("type") == "submit_booking":
            if not bool(ctx.get("booking_pending")):
                reply = (
                    "Сейчас нет активной заявки на бронь. "
                    "Сначала получите рекомендации и выберите ресторан."
                )
                await self.state_repository.append_history(
                    session_id=session_id,
                    messages=messages,
                    reply=reply,
                )
                updated_state = await self.state_repository.get_state_for_session(session_id)
                return {"reply": reply, "session_id": session_id, "state": updated_state.to_dict()}
            form_booking_payload = {
                "starts_at": str(client_action.get("starts_at") or "").strip(),
                "guest_count": int(client_action["guest_count"]),
                "guest_name": str(client_action.get("guest_name") or "").strip(),
                "guest_phone": str(client_action.get("guest_phone") or "").strip(),
            }

        llm_client, system_prompt, node_params = self.llm_registry.get_default_node()

        def _trace_append(state: RecState, stage: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
            tr: List[Dict[str, Any]] = list(state.get("pipeline_trace") or [])
            tr.append(
                {
                    "stage": stage,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "payload": payload,
                }
            )
            return tr[-200:]

        def _last_user_text() -> str:
            # Prefer last user message; messages are already provided by /api/dialog.
            for m in reversed(messages):
                if m.get("role") == "user" and m.get("content"):
                    return str(m["content"])
            # fallback to any content
            return str(messages[-1].get("content") if messages else "")

        def _user_dialog_text_for_requirements_extraction() -> str:
            # Fix "key problem 2": requirements extraction should use the whole
            # user history present in this dialog request (not only last message).
            user_msgs: List[Dict[str, Any]] = [
                m for m in messages if m.get("role") == "user" and m.get("content") is not None
            ]
            if not user_msgs:
                return _last_user_text()
            parts: List[str] = []
            for i, m in enumerate(user_msgs, start=1):
                parts.append(f"Пользовательское сообщение {i}:\n{str(m.get('content'))}")
            return "\n\n".join(parts)

        async def extract_requirements_node(state: RecState) -> RecState:
            if form_booking_payload is not None:
                return {
                    **state,
                    "current_node": "extract_requirements",
                    "pipeline_trace": _trace_append(
                        state,
                        "extract_requirements",
                        {"skipped_llm": True, "reason": "submit_booking_form"},
                    ),
                }

            prev_req: Dict[str, Any] = dict(state.get("recommendation_requirements") or {})
            prev_missing_fields: List[str] = [
                str(x) for x in (state.get("missing_fields") or []) if isinstance(x, str)
            ]

            def _is_num(v: Any) -> bool:
                return isinstance(v, (int, float)) and not isinstance(v, bool)

            def _valid_stored_budget(b: Any) -> bool:
                if not isinstance(b, dict):
                    return False
                mn, mx = b.get("min"), b.get("max")
                if not (_is_num(mn) and _is_num(mx)):
                    return False
                return float(mn) >= 0 and float(mx) >= float(mn)

            def _missing_fields_for_req(req: Dict[str, Any]) -> List[str]:
                out: List[str] = []
                c = req.get("city")
                city_has = isinstance(c, str) and bool(c.strip())
                if not city_has:
                    out.append("city")
                ps = req.get("party_size")
                ps_ok = False
                if _is_num(ps):
                    ps_ok = int(ps) >= 1
                if not ps_ok:
                    out.append("party_size")
                if not _valid_stored_budget(req.get("budget_range")):
                    out.append("budget_range")
                loc = req.get("location")
                loc_ok = False
                if isinstance(loc, dict):
                    lt = loc.get("type")
                    lv = loc.get("value")
                    if lt in {"metro", "area"} and isinstance(lv, str) and lv.strip():
                        loc_ok = True
                    elif lt == "none":
                        loc_ok = True
                if city_has and not loc_ok:
                    out.append("location_or_metro")
                slug = req.get("city_slug")
                slug_ok = isinstance(slug, str) and bool(slug.strip())
                if city_has and not slug_ok:
                    out.append("city_slug")
                return out

            if client_action and client_action.get("type") == "confirm_search_plan":
                if bool(state.get("booking_pending")):
                    return {
                        **state,
                        "current_node": "extract_requirements",
                        "pipeline_trace": _trace_append(
                            state,
                            "extract_requirements",
                            {"skipped_llm": True, "reason": "confirm_ignored_booking_pending"},
                        ),
                    }
                req0 = dict(state.get("recommendation_requirements") or {})
                miss0 = _missing_fields_for_req(req0)
                if miss0:
                    return {
                        **state,
                        "current_node": "extract_requirements",
                        "requirements_complete": False,
                        "missing_fields": miss0,
                        "search_plan_confirmed": bool(state.get("search_plan_confirmed")),
                        "search_plan_fingerprint": state.get("search_plan_fingerprint"),
                        "pipeline_trace": _trace_append(
                            state,
                            "extract_requirements",
                            {"confirm_rejected": True, "missing_fields": miss0},
                        ),
                    }
                fp0 = fingerprint_search_plan(req0)
                return {
                    **state,
                    "current_node": "extract_requirements",
                    "recommendation_requirements": req0,
                    "requirements_complete": True,
                    "missing_fields": [],
                    "search_plan_confirmed": True,
                    "search_plan_fingerprint": fp0,
                    "pipeline_trace": _trace_append(
                        state,
                        "extract_requirements",
                        {"skipped_llm": True, "reason": "confirm_search_plan", "fingerprint": fp0},
                    ),
                }

            user_dialog_text = _user_dialog_text_for_requirements_extraction()
            prompt_sys = (
                "Ты извлекаешь требования пользователя для подбора ресторана. "
                "Верни ТОЛЬКО JSON без markdown.\n\n"
                "КРИТИЧНО — не домысливай. Заполняй поле только если пользователь явно сказал это в истории сообщений "
                "(дословно или однозначной фразой). Запрещено: угадывать город по метро/району, число гостей по поводу "
                "(«годовщина» не означает «двое»), бюджет «типичный», данные из стереотипов или общих знаний.\n"
                "Если пользователь не сказал — ставь null / опускай значение / для budget_range не придумывай min/max.\n"
                "Поле city — только если назван город словами пользователя (например «Москва», «в Питере»). "
                "Одна только станция метро или район без названия города — это location, не заполняй city.\n"
                "party_size — только при явном числе или явной формулировке числа гостей («нас двое», «на 5 человек», «вчетвером»). "
                "Без этого — null.\n"
                "budget_range — только если пользователь явно назвал суммы (min/max в рублях). "
                "Считай это **общим бюджетом на всю компанию** (всех гостей на визит), если пользователь явно не сказал «на человека». Иначе null.\n"
                "location — type metro|area и value, если пользователь назвал станцию, линию, район, улицу как ориентир; "
                "type none и value null, если локацию не называли.\n"
                "occasion — опционально, только если пользователь сам назвал повод (для контекста/UI); на поиск и ранжирование не влияет.\n\n"
                "Поля requirements_complete и missing_fields в ответе игнорируются — их пересчитает система; всё равно заполни честно по правилам выше.\n\n"
                "Структура JSON:\n"
                "- requirements_complete: true/false (вспомогательно для модели)\n"
                "- missing_fields: массив строк (вспомогательно)\n"
                "- recommendation_requirements:\n"
                "  city: string|null\n"
                "  city_slug: всегда null — префикс города для afisha.ru вычисляет система из city, не заполняй.\n"
                "  location: {type: 'metro'|'area'|'none', value: string|null}|null\n"
                "  budget_range: {min: number, max: number}|null\n"
                "  party_size: number|null\n"
                "  occasion: string|null\n"
                "  must_have: массив строк — только явно запрошенные опции (parking и т.д.)\n"
                "  cuisine_wanted / cuisine_avoid: массивы — только явно сказанное\n"
            )
            user_msg = (
                "ДИАЛОГ ПОЛЬЗОВАТЕЛЯ (история сообщений) — единственный источник фактов. "
                "Не добавляй факты из головы; станция без города не превращается в city.\n\n"
                f"{user_dialog_text}\n\n"
                "Ранее сохранённые требования (только чтобы не потерять уже названные пользователем в прошлых ходах сессии; "
                "не копируй оттуда то, чего нет в истории сообщений выше):\n"
                f"{json.dumps(state.get('recommendation_requirements', {}), ensure_ascii=False)}"
            )

            params_req = {**node_params, "response_format": {"type": "json_object"}}
            try:
                raw = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": prompt_sys},
                        {"role": "user", "content": user_msg},
                    ],
                    **params_req,
                )
            except Exception:
                raw = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": prompt_sys},
                        {"role": "user", "content": user_msg},
                    ],
                    **node_params,
                )

            # parse JSON
            try:
                json_text = raw
                # best effort: first {...}
                start = json_text.find("{")
                end = json_text.rfind("}")
                if start >= 0 and end > start:
                    json_text = json_text[start : end + 1]
                parsed = json.loads(json_text)
            except Exception:
                prev_fixed = dict(prev_req)
                _pc = prev_fixed.get("city")
                prev_fixed["city_slug"] = resolve_afisha_city_slug(
                    _pc if isinstance(_pc, str) else None
                )
                miss = _missing_fields_for_req(prev_fixed)
                fp_fail = fingerprint_search_plan(prev_fixed)
                return {
                    **state,
                    "current_node": "extract_requirements",
                    "requirements_complete": len(miss) == 0,
                    "missing_fields": miss,
                    "repeated_missing_fields": [x for x in miss if x in set(prev_missing_fields)],
                    "recommendation_requirements": prev_fixed,
                    "search_plan_confirmed": False,
                    "search_plan_fingerprint": fp_fail,
                    "pipeline_trace": _trace_append(
                        state,
                        "extract_requirements",
                        {"error": "json_parse_failed", "missing_fields": miss},
                    ),
                }

            # Deterministic validation (Fix "key problem 3"):
            # never trust requirements_complete/missing_fields from the model.
            llm_req = parsed.get("recommendation_requirements") or {}
            if not isinstance(llm_req, dict):
                llm_req = {}

            def _is_number(v: Any) -> bool:
                return isinstance(v, (int, float)) and not isinstance(v, bool)

            def _norm_budget_range(v: Any) -> Optional[Dict[str, Any]]:
                if not isinstance(v, dict):
                    return None
                mn = v.get("min")
                mx = v.get("max")
                if not (_is_number(mn) and _is_number(mx)):
                    return None
                mn_f = float(mn)
                mx_f = float(mx)
                if mn_f < 0 or mx_f < mn_f:
                    return None
                return {"min": mn_f, "max": mx_f}

            budget_range = _norm_budget_range(llm_req.get("budget_range"))

            location = llm_req.get("location")
            location_ok = False
            norm_location: Optional[Dict[str, Any]] = None
            if isinstance(location, dict):
                loc_type = location.get("type")
                loc_val = location.get("value")
                if loc_type in {"metro", "area"} and isinstance(loc_val, str) and loc_val.strip():
                    location_ok = True
                    norm_location = {"type": loc_type, "value": loc_val.strip()}
                elif loc_type == "none":
                    location_ok = True
                    norm_location = {"type": "none", "value": None}

            occasion = llm_req.get("occasion")
            occasion_ok = isinstance(occasion, str) and bool(occasion.strip())

            # Normalize some optional fields so downstream nodes can rely on types.
            def _norm_str_list(v: Any) -> List[str]:
                if not isinstance(v, list):
                    return []
                out: List[str] = []
                for x in v:
                    if isinstance(x, str):
                        s = x.strip()
                        if s:
                            out.append(s)
                return out

            city_raw = llm_req.get("city")
            city_ok = isinstance(city_raw, str) and bool(city_raw.strip())
            city_norm = city_raw.strip() if city_ok else None

            party_size = llm_req.get("party_size")
            if party_size is None:
                party_size_norm: Optional[int] = None
            elif _is_number(party_size):
                party_size_norm = int(party_size)
            else:
                party_size_norm = None

            # Merge LLM output with persisted requirements so one failed/partial JSON
            # does not wipe fields already collected in earlier turns.
            merged_city = (
                city_norm
                if city_ok
                else (
                    prev_req.get("city")
                    if isinstance(prev_req.get("city"), str) and prev_req["city"].strip()
                    else None
                )
            )
            merged_party: Optional[int] = None
            if party_size_norm is not None and party_size_norm >= 1:
                merged_party = party_size_norm
            else:
                pp = prev_req.get("party_size")
                if _is_number(pp):
                    merged_party = int(pp)
                if merged_party is not None and merged_party < 1:
                    merged_party = None

            merged_occasion: Optional[str] = None
            if occasion_ok:
                merged_occasion = occasion.strip()
            else:
                po = prev_req.get("occasion")
                if isinstance(po, str) and po.strip():
                    merged_occasion = po.strip()

            merged_location: Optional[Dict[str, Any]] = None
            if location_ok and norm_location is not None:
                merged_location = norm_location
            else:
                pl = prev_req.get("location")
                merged_location = pl if isinstance(pl, dict) else None

            mh_new = _norm_str_list(llm_req.get("must_have"))
            cw_new = _norm_str_list(llm_req.get("cuisine_wanted"))
            ca_new = _norm_str_list(llm_req.get("cuisine_avoid"))
            merged_must = mh_new if mh_new else list(prev_req.get("must_have") or [])
            merged_cw = cw_new if cw_new else list(prev_req.get("cuisine_wanted") or [])
            merged_ca = ca_new if ca_new else list(prev_req.get("cuisine_avoid") or [])

            # Afisha avg_check is per person; normalize party total -> per person for ranking.
            merged_budget: Optional[Dict[str, Any]] = None
            if budget_range is not None:
                ps_div = merged_party if merged_party is not None and merged_party >= 1 else 1
                merged_budget = {
                    "min": float(budget_range["min"]) / float(ps_div),
                    "max": float(budget_range["max"]) / float(ps_div),
                }
            else:
                pb = prev_req.get("budget_range")
                merged_budget = pb if _valid_stored_budget(pb) else None

            resolved_city_slug = (
                resolve_afisha_city_slug(merged_city) if merged_city else None
            )

            normalized: Dict[str, Any] = {
                "city": merged_city,
                "city_slug": resolved_city_slug,
                "location": merged_location,
                "budget_range": merged_budget,
                "party_size": merged_party,
                "occasion": merged_occasion,
                "must_have": merged_must,
                "cuisine_wanted": merged_cw,
                "cuisine_avoid": merged_ca,
            }

            missing_fields = _missing_fields_for_req(normalized)
            requirements_complete = len(missing_fields) == 0
            repeated_missing_fields = [x for x in missing_fields if x in set(prev_missing_fields)]

            new_fp = fingerprint_search_plan(normalized)
            old_fp = state.get("search_plan_fingerprint")
            had_confirmed = bool(state.get("search_plan_confirmed"))
            if had_confirmed and old_fp is not None and new_fp != old_fp:
                confirmed_out = False
            else:
                confirmed_out = had_confirmed

            return {
                **state,
                "current_node": "extract_requirements",
                "recommendation_requirements": normalized,
                "requirements_complete": requirements_complete,
                "missing_fields": missing_fields,
                "repeated_missing_fields": repeated_missing_fields,
                "search_plan_confirmed": confirmed_out,
                "search_plan_fingerprint": new_fp,
                "pipeline_trace": _trace_append(
                    state,
                    "extract_requirements",
                    {
                        "requirements_complete": requirements_complete,
                        "missing_fields": missing_fields,
                        "city": merged_city,
                        "city_slug": resolved_city_slug,
                        "party_size": merged_party,
                        "merged_with_previous": True,
                        "search_plan_fingerprint": new_fp,
                        "search_plan_confirmed": confirmed_out,
                    },
                ),
            }

        async def confirm_search_plan_node(state: RecState) -> RecState:
            req = state.get("recommendation_requirements") or {}
            state["reply"] = format_search_plan_summary(req if isinstance(req, dict) else {})
            state["current_node"] = "confirm_search_plan"
            state["pipeline_trace"] = _trace_append(
                state,
                "confirm_search_plan",
                {"fingerprint": fingerprint_search_plan(req if isinstance(req, dict) else {})},
            )
            return state

        async def ask_questions_node(state: RecState) -> RecState:
            missing = state.get("missing_fields") or [
                "city",
                "party_size",
                "budget_range",
                "location_or_metro",
            ]
            repeated_missing = [
                str(x) for x in (state.get("repeated_missing_fields") or []) if isinstance(x, str)
            ]
            prompt_count = int(state.get("requirements_prompt_count") or 0)
            field_labels: Dict[str, str] = {
                "city": "в каком городе ищем ресторан",
                "city_slug": "уточните название города (как в обычном адресе), чтобы найти рестораны в каталоге Афиши",
                "party_size": "сколько человек будет (число участников)",
                "budget_range": "общий бюджет на компанию в рублях (от … до … на весь визит)",
                "location_or_metro": "район или станция метро, где удобно",
            }
            required_lines = [f"- {field_labels.get(code, code)}" for code in missing]
            repeated_lines = [f"- {field_labels.get(code, code)}" for code in repeated_missing]
            mandatory_fields = [
                "город",
                "количество гостей",
                "общий бюджет на компанию",
                "район или метро",
            ]
            prompt_sys = (
                "Ты дружелюбный ассистент по подбору ресторанов. "
                "Сформулируй одно короткое человеческое уточнение пользователю на русском языке (2-5 предложений), "
                "без сухого канцелярита и без дословного повторения предыдущего вопроса.\n"
                "Обязательно объясни, что есть обязательный минимум полей для эффективного поиска, "
                "и почему он нужен (чтобы сузить выдачу и избежать нерелевантных вариантов).\n"
                "Если есть repeated_missing, мягко укажи, что эти поля не удалось однозначно понять из прошлого ответа, "
                "и попроси уточнить их снова.\n"
                "Запрашивай только поля из missing. В конце добавь фразу, что можно ответить одним сообщением."
            )
            prompt_user = (
                f"Это {'первый' if prompt_count == 0 else 'повторный'} запрос уточнений.\n"
                f"Обязательный минимум полей: {', '.join(mandatory_fields)}.\n"
                "Сейчас нужно уточнить:\n"
                + "\n".join(required_lines)
                + "\n\n"
                + (
                    "Поля, которые уже спрашивали и не удалось однозначно извлечь:\n"
                    + "\n".join(repeated_lines)
                    + "\n\n"
                    if repeated_lines
                    else ""
                )
                + "Сформулируй ответ для пользователя."
            )
            try:
                reply = (
                    await llm_client.chat(
                        messages=[
                            {"role": "system", "content": prompt_sys},
                            {"role": "user", "content": prompt_user},
                        ],
                        **node_params,
                    )
                ).strip()
            except Exception:
                reply = ""

            if not reply:
                intro = (
                    "Чтобы эффективно подобрать подходящие рестораны, нужен обязательный минимум данных: "
                    "город, количество гостей, бюджет и район/метро.\n"
                )
                if repeated_lines:
                    intro += (
                        "Часть параметров из прошлого ответа не удалось определить однозначно, уточните, пожалуйста:\n"
                    )
                else:
                    intro += "Уточните, пожалуйста:\n"
                reply = intro + "\n".join(required_lines) + "\n\nМожно ответить одним сообщением со всеми пунктами."
            state["reply"] = reply
            state["current_node"] = "ask_questions"
            state["requirements_prompt_count"] = prompt_count + 1
            state["pipeline_trace"] = _trace_append(
                state,
                "ask_questions",
                {
                    "missing_fields": missing,
                    "repeated_missing_fields": repeated_missing,
                    "requirements_prompt_count": prompt_count + 1,
                },
            )
            return state

        async def build_yandex_queries_node(state: RecState) -> RecState:
            req = state.get("recommendation_requirements") or {}
            city_slug = (req.get("city_slug") or "").strip() if isinstance(req.get("city_slug"), str) else ""
            city = req.get("city") or ""
            location = req.get("location") or {}
            loc_type = location.get("type") if isinstance(location, dict) else None
            loc_val = location.get("value") if isinstance(location, dict) else None

            cuisine_terms = []
            for x in (req.get("cuisine_wanted") or [])[:3]:
                if x:
                    cuisine_terms.append(str(x))
            cuisine_part = " ".join(cuisine_terms) if cuisine_terms else ""

            loc_part = f"\"{loc_val}\"" if loc_val else ""
            if not city_slug:
                return {
                    **state,
                    "current_node": "build_yandex_queries",
                    "yandex_queries": [],
                    "pipeline_trace": _trace_append(
                        state,
                        "build_yandex_queries",
                        {
                            "queries": [],
                            "skipped": True,
                            "reason": "empty_city_slug",
                            "city": (city[:120] if isinstance(city, str) else None),
                        },
                    ),
                }

            # Prefer restaurant-card pages containing метро names in heading.
            base = f"site:afisha.ru {city_slug}/restaurant"

            q1 = " ".join([base, loc_part, cuisine_part]).strip()
            q2 = " ".join([base, loc_part, "Средний чек", cuisine_part]).strip()
            q3 = " ".join([base, loc_part, "Открыто", cuisine_part]).strip()
            q4 = " ".join([base, loc_part, "ресторан", cuisine_part]).strip()

            queries = [q for q in [q1, q2, q3, q4] if len(q) >= 10]
            # dedupe preserve order
            seen = set()
            unique = []
            for q in queries:
                if q in seen:
                    continue
                seen.add(q)
                unique.append(q)
            qfinal = unique[:5]
            return {
                **state,
                "current_node": "build_yandex_queries",
                "yandex_queries": qfinal,
                "pipeline_trace": _trace_append(state, "build_yandex_queries", {"queries": qfinal}),
            }

        async def yandex_web_search_node(state: RecState) -> RecState:
            from .yandex_web_search import YandexWebSearchClient

            urls: List[str] = []
            errors = list(state.get("service_errors") or [])
            try:
                client = YandexWebSearchClient.from_env()
            except Exception as exc:
                errors.append(str(exc))
                return {
                    **state,
                    "current_node": "yandex_web_search",
                    "yandex_urls": [],
                    "service_errors": errors,
                    "pipeline_trace": _trace_append(
                        state,
                        "yandex_web_search",
                        {"url_count": 0, "error": str(exc)},
                    ),
                }

            for q in state.get("yandex_queries") or []:
                try:
                    found = await client.search(q, page=0, max_docs=50)
                    urls.extend(found)
                except Exception as exc:
                    errors.append(f"Yandex search failed for query '{q}': {exc}")
            return {
                **state,
                "current_node": "yandex_web_search",
                "yandex_urls": urls,
                "service_errors": errors,
                "pipeline_trace": _trace_append(
                    state,
                    "yandex_web_search",
                    {"url_count": len(urls), "errors": len(errors)},
                ),
            }

        async def dedupe_and_filter_urls_node(state: RecState) -> RecState:
            from .afisha_urls import filter_and_order_afisha_restaurant_urls

            urls = state.get("yandex_urls") or []
            # Canonical Afisha cards only; direct SERP links — cache wrappers and other hosts are dropped.
            out = filter_and_order_afisha_restaurant_urls(urls)
            trimmed = out[:80]
            return {
                **state,
                "current_node": "dedupe_and_filter_urls",
                "yandex_urls": trimmed,
                "pipeline_trace": _trace_append(
                    state,
                    "dedupe_and_filter_urls",
                    {"url_count": len(trimmed), "sample": trimmed[:8]},
                ),
            }

        async def fetch_afisha_cards_node(state: RecState) -> RecState:
            from .afisha_parser import fetch_and_parse_afisha_card
            from .afisha_urls import filter_and_order_afisha_restaurant_urls

            candidates: List[Dict[str, Any]] = []
            skipped_closed = 0
            urls = filter_and_order_afisha_restaurant_urls(list(state.get("yandex_urls") or []))[
                :30
            ]
            for url in urls:
                try:
                    cand = await fetch_and_parse_afisha_card(url)
                    # basic sanity
                    if not cand.get("name") and not cand.get("metro"):
                        continue
                    if cand.get("venue_closed"):
                        skipped_closed += 1
                        continue
                    candidates.append(cand)
                except Exception:
                    continue
            return {
                **state,
                "current_node": "fetch_afisha_cards",
                "candidates": candidates,
                "pipeline_trace": _trace_append(
                    state,
                    "fetch_afisha_cards",
                    {
                        "candidate_count": len(candidates),
                        "skipped_closed": skipped_closed,
                        "names": [c.get("name") for c in candidates[:10]],
                    },
                ),
            }

        async def toka_capacity_gate_node(state: RecState) -> RecState:
            from .toka_candidate_capacity import apply_toka_capacity_gate

            req = state.get("recommendation_requirements") or {}
            ps = req.get("party_size")
            try:
                party_n = int(ps) if ps is not None and not isinstance(ps, bool) else 1
            except Exception:
                party_n = 1
            party_n = max(1, party_n)

            candidates = list(state.get("candidates") or [])
            gated, extra_err = await apply_toka_capacity_gate(candidates, party_n)
            errors = list(state.get("service_errors") or [])
            errors.extend(extra_err)

            return {
                **state,
                "current_node": "toka_capacity_gate",
                "candidates": gated,
                "service_errors": errors,
                "pipeline_trace": _trace_append(
                    state,
                    "toka_capacity_gate",
                    {
                        "in_count": len(candidates),
                        "out_count": len(gated),
                        "party_size": party_n,
                    },
                ),
            }

        async def formal_rank_node(state: RecState) -> RecState:
            from .recommendation_ranker import rank_candidates, recalc_formal_thresholds
            from .toka_candidate_capacity import apply_toka_unverified_score_penalty

            requirements = state.get("recommendation_requirements") or {}
            candidates = state.get("candidates") or []

            ranked = rank_candidates(candidates, requirements)
            sc = ranked.get("scored_candidates") or []
            apply_toka_unverified_score_penalty(sc)
            min_s, above_n = recalc_formal_thresholds(sc)
            return {
                **state,
                "current_node": "formal_rank",
                "scored_candidates": sc,
                "min_score": min_s,
                "above_threshold_count": above_n,
                "pipeline_trace": _trace_append(
                    state,
                    "formal_rank",
                    {
                        "min_score": min_s,
                        "above_threshold_count": above_n,
                        "top_preview": [
                            {"url": x.get("url"), "formal_score": x.get("formal_score")}
                            for x in sorted(
                                [c for c in sc if not c.get("hard_pass")],
                                key=lambda x: float(x.get("formal_score") or 0),
                                reverse=True,
                            )[:5]
                        ],
                    },
                ),
            }

        async def take_shortlist_node(state: RecState) -> RecState:
            min_score = float(state.get("min_score") or 0.0)
            scored = state.get("scored_candidates") or []

            above = [c for c in scored if (not c.get("hard_pass")) and float(c.get("formal_score") or 0) >= min_score]
            above_sorted = sorted(above, key=lambda x: float(x.get("formal_score") or 0), reverse=True)
            shortlist = above_sorted[:5]
            return {
                **state,
                "current_node": "take_shortlist",
                "shortlist": shortlist,
                "pipeline_trace": _trace_append(
                    state,
                    "take_shortlist",
                    {
                        "shortlist_size": len(shortlist),
                        "urls": [x.get("url") for x in shortlist],
                    },
                ),
            }

        async def relax_criteria_fallback_node(state: RecState) -> RecState:
            relax_attempts = int(state.get("relax_attempts") or 0)
            req = state.get("recommendation_requirements") or {}

            # Prevent infinite loops
            if relax_attempts >= 2:
                # stop relaxing; keep requirements
                return {
                    **state,
                    "current_node": "relax_fallback",
                    "reply": "Не удалось найти подходящие рестораны по доступным данным.",
                    "pipeline_trace": _trace_append(
                        state,
                        "relax_fallback",
                        {"halt": True, "attempts": relax_attempts},
                    ),
                }

            # Broadening: remove metro/area constraint => use city-wide discovery.
            location = req.get("location")
            if isinstance(location, dict) and location.get("type") in {"metro", "area"}:
                req["location"] = {"type": "none", "value": None}

            # Expand budget by +15%
            budget = req.get("budget_range") or {}
            if "min" in budget and "max" in budget and isinstance(budget["min"], (int, float)):
                bmin = int(budget["min"])
                bmax = int(budget["max"])
                req["budget_range"] = {"min": int(bmin * 0.9), "max": int(bmax * 1.15)}

            # Soften cuisine preferences slightly (remove avoid for fallback)
            if relax_attempts == 0:
                req["cuisine_avoid"] = []

            return {
                **state,
                "current_node": "relax_fallback",
                "relax_attempts": relax_attempts + 1,
                "recommendation_requirements": req,
                "pipeline_trace": _trace_append(
                    state,
                    "relax_fallback",
                    {
                        "attempt": relax_attempts + 1,
                        "location_type_after": (req.get("location") or {}).get("type"),
                    },
                ),
            }

        async def fetch_and_analyze_reviews_shortlist_node(state: RecState) -> RecState:
            from .afisha_reviews_parser import fetch_and_extract_reviews
            from .review_aspect_extractor import extract_aspects_from_reviews

            shortlist = state.get("shortlist") or []
            reviews_aspects: List[Dict[str, Any]] = []
            for item in shortlist:
                url = item.get("url")
                if not url:
                    continue
                try:
                    reviews = await fetch_and_extract_reviews(url)
                    aspects = await extract_aspects_from_reviews(
                        llm_client,
                        node_params,
                        reviews=reviews,
                        restaurant_name=item.get("name"),
                    )
                    reviews_aspects.append({"url": url, "aspects": aspects.get("aspects"), "evidence_count": aspects.get("evidence_count", 0)})
                except Exception:
                    continue
            return {
                **state,
                "current_node": "fetch_and_analyze_reviews",
                "reviews_aspects": reviews_aspects,
                "pipeline_trace": _trace_append(
                    state,
                    "fetch_and_analyze_reviews",
                    {"shortlist_size": len(shortlist), "aspects_count": len(reviews_aspects)},
                ),
            }

        async def final_rerank_and_explain_node(state: RecState) -> RecState:
            shortlist = state.get("shortlist") or []
            reviews_aspects = state.get("reviews_aspects") or []
            aspects_by_url = {a.get("url"): a for a in reviews_aspects if a.get("url")}

            def reviews_bonus(aspects: Optional[Dict[str, Any]]) -> float:
                if not aspects:
                    return 0.0
                return 0.1 * float(aspects.get("food", {}).get("score") or 0.0) + 0.06 * float(
                    aspects.get("service", {}).get("score") or 0.0
                ) + 0.04 * float(aspects.get("ambience", {}).get("score") or 0.0)

            final: List[Dict[str, Any]] = []
            for item in shortlist:
                url = item.get("url")
                base = float(item.get("formal_score") or 0.0)
                aspects = aspects_by_url.get(url, {}).get("aspects")
                bonus = reviews_bonus(aspects)
                final_score = max(0.0, min(1.0, base * 0.75 + bonus))

                # Build evidence-based explanation
                reasons: List[str] = []
                if item.get("budget_score") is not None and float(item["budget_score"]) > 0:
                    reasons.append("соответствует бюджету (по среднему чеку)");
                if item.get("cuisine_score") is not None and float(item["cuisine_score"]) > 0:
                    reasons.append("совпадает по кухне/тегам");

                ev = None
                if aspects:
                    # pick the highest-scoring evidence among relevant aspects
                    candidates = [
                        ("еда", aspects.get("food", {})),
                        ("сервис", aspects.get("service", {})),
                        ("атмосфера", aspects.get("ambience", {})),
                        ("тихо/шумно", aspects.get("noise", {})),
                        ("ценность", aspects.get("value", {})),
                    ]
                    candidates = sorted(
                        candidates,
                        key=lambda x: float(x[1].get("score") or 0.0),
                        reverse=True,
                    )
                    if candidates and candidates[0][1].get("evidence"):
                        ev = candidates[0][1].get("evidence")
                if ev:
                    reasons.append(f"отзывы: {ev}")

                final.append(
                    {
                        **item,
                        "final_score": final_score,
                        "explanation": reasons[:3],
                    }
                )

            final_sorted = sorted(final, key=lambda x: float(x.get("final_score") or 0.0), reverse=True)
            top5 = final_sorted[:5]
            return {
                **state,
                "current_node": "final_rerank_and_explain",
                "final_recommendations": top5,
                "recommendations": top5,
                "pipeline_trace": _trace_append(
                    state,
                    "final_rerank_and_explain",
                    {
                        "top5": [
                            {"url": x.get("url"), "name": x.get("name"), "final_score": x.get("final_score")}
                            for x in top5
                        ],
                    },
                ),
            }

        async def format_reply_node(state: RecState) -> RecState:
            candidates = state.get("final_recommendations") or state.get("shortlist") or []
            if not candidates:
                errors = state.get("service_errors") or []
                if errors:
                    reply = (
                        "Сейчас не удалось выполнить веб-поиск ресторанов (проблема с конфигурацией сервиса поиска). "
                        "Проверьте переменные окружения YANDEX_SEARCH_URL, YANDEX_SEARCH_API_KEY_ID, YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID."
                    )
                else:
                    reply = "В этой локации с заданными критериями подходящих ресторанов не нашлось. Попробовать расширить локацию или бюджет?"
                state["reply"] = reply
                state["booking_pending"] = False
                state["search_plan_confirmed"] = False
                state["current_node"] = "format_reply"
                state["pipeline_trace"] = _trace_append(
                    state, "format_reply", {"empty_candidates": True, "had_service_errors": bool(errors)}
                )
                return state

            n = len(candidates)
            # Short intro: details and links are shown as structured cards in the SPA.
            if n == 1:
                state["reply"] = (
                    "Подобрал вариант — смотрите карточку ниже. Можно сразу забронировать столик."
                )
            else:
                state["reply"] = (
                    "Вот несколько вариантов — выберите ресторан для брони в списке ниже."
                )
            # Switch dialog into booking mode after recommendations.
            state["booking_pending"] = True
            state["booking_selected_candidate"] = candidates[0]
            state["booking_requirements"] = state.get("booking_requirements") or {}
            state["booking_complete"] = False
            state["booking_missing_fields"] = ["starts_at", "guest_count", "guest_name", "guest_phone"]
            state["booking_errors"] = []
            state["current_node"] = "format_reply"
            state["search_plan_confirmed"] = False
            state["pipeline_trace"] = _trace_append(
                state,
                "format_reply",
                {"candidate_count": n, "booking_prompt_appended": True},
            )
            return state

        async def extract_booking_requirements_node(state: RecState) -> RecState:
            """
            Extract booking parameters from the latest user message.

            This node is used when `booking_pending=true` (set right after recommendations).
            """

            def _norm_iso_datetime(v: Any) -> Optional[str]:
                if not isinstance(v, str):
                    return None
                s = v.strip()
                if not any(t in s for t in ["T", "-"]):
                    return None
                return s

            if form_booking_payload is not None:
                fb = form_booking_payload
                starts_at = _norm_iso_datetime(str(fb.get("starts_at") or ""))
                missing_fields_fb: List[str] = []
                if not starts_at:
                    missing_fields_fb.append("starts_at")
                gc_raw = fb.get("guest_count")
                guest_count_norm_fb: Optional[int] = None
                if isinstance(gc_raw, (int, float)) and not isinstance(gc_raw, bool):
                    if int(gc_raw) >= 1:
                        guest_count_norm_fb = int(gc_raw)
                if guest_count_norm_fb is None:
                    missing_fields_fb.append("guest_count")
                gn_fb = fb.get("guest_name")
                if not (isinstance(gn_fb, str) and gn_fb.strip()):
                    missing_fields_fb.append("guest_name")
                gp_fb = fb.get("guest_phone")
                if not (isinstance(gp_fb, str) and gp_fb.strip()):
                    missing_fields_fb.append("guest_phone")
                booking_complete_fb = len(missing_fields_fb) == 0
                normalized_fb: Dict[str, Any] = {
                    "starts_at": starts_at,
                    "duration_minutes": 120,
                    "guest_name": gn_fb.strip() if isinstance(gn_fb, str) else None,
                    "guest_phone": gp_fb.strip() if isinstance(gp_fb, str) else None,
                    "guest_count": guest_count_norm_fb,
                    "notes": "",
                }
                return {
                    **state,
                    "current_node": "extract_booking_requirements",
                    "booking_pending": True,
                    "booking_complete": booking_complete_fb,
                    "booking_missing_fields": missing_fields_fb,
                    "booking_requirements": normalized_fb,
                    "pipeline_trace": _trace_append(
                        state,
                        "extract_booking_requirements",
                        {
                            "source": "submit_booking_form",
                            "booking_complete": booking_complete_fb,
                            "missing": missing_fields_fb,
                        },
                    ),
                }

            return {
                **state,
                "current_node": "extract_booking_requirements",
                "booking_pending": True,
                "booking_complete": False,
                "booking_missing_fields": ["starts_at", "guest_count", "guest_name", "guest_phone"],
                "booking_requirements": state.get("booking_requirements") or {},
                "pipeline_trace": _trace_append(
                    state,
                    "extract_booking_requirements",
                    {"source": "no_form_payload", "booking_complete": False},
                ),
            }

        async def ask_booking_questions_node(state: RecState) -> RecState:
            missing = state.get("booking_missing_fields") or ["starts_at", "guest_count", "guest_name", "guest_phone"]
            field_to_line = {
                "starts_at": "дату и время старта в ISO-формате (например: `2026-04-02T19:00:00Z`)",
                "guest_count": "количество гостей",
                "guest_name": "имя для брони",
                "guest_phone": "телефон для связи",
            }
            lines = [f"- {field_to_line.get(x, x)}" for x in missing]
            state["reply"] = (
                "Заполните данные брони в форме ниже или уточните в сообщении:\n"
                + "\n".join(lines)
            )
            state["current_node"] = "ask_booking_questions"
            state["booking_pending"] = True
            state["pipeline_trace"] = _trace_append(state, "ask_booking_questions", {"missing": missing})
            return state

        async def create_reservation_node(state: RecState) -> RecState:
            req = state.get("booking_requirements") or {}
            booking_errors: List[str] = []
            try:
                from ..services.toka_gateway import get_toka_gateway

                gateway = await get_toka_gateway()
                starts_at = str(req.get("starts_at"))
                guest_count = int(req.get("guest_count"))
                reservation_data = await gateway.create_reservation(
                    restaurant_ref=dict(state.get("booking_selected_candidate") or {}),
                    starts_at=starts_at,
                    duration_minutes=int(req.get("duration_minutes") or 120),
                    guest_name=str(req.get("guest_name") or ""),
                    guest_phone=str(req.get("guest_phone") or ""),
                    guest_count=guest_count,
                    notes=str(req.get("notes") or ""),
                )
                reservation = reservation_data.get("raw") or reservation_data
                state["reservation_result"] = reservation
                state["booking_pending"] = False
                state["booking_complete"] = True
                state["booking_missing_fields"] = []
                state["booking_errors"] = []

                reservation_id = (
                    reservation.get("id")
                    or reservation.get("reservation_id")
                    or reservation.get("code")
                    or None
                )
                rid_part = f" (id: {reservation_id})" if reservation_id else ""
                state["reply"] = (
                    "Готово — бронирование создано. "
                    f"Ресторан: {state.get('booking_selected_candidate', {}).get('name') or '—'}. "
                    f"Дата/время: {starts_at}. "
                    f"Гостей: {guest_count}.{rid_part}"
                )
                state["current_node"] = "create_reservation"
                state["pipeline_trace"] = _trace_append(
                    state,
                    "create_reservation",
                    {
                        "ok": True,
                        "reservation_id": reservation_id,
                        "store_id": (reservation_data.get("resolved") or {}).get("store_id"),
                    },
                )
                return state
            except Exception as exc:
                booking_errors.append(str(exc))
                state["booking_pending"] = True
                state["booking_complete"] = False
                state["booking_errors"] = booking_errors
                state["current_node"] = "create_reservation_error"
                state["reply"] = (
                    "Не удалось создать бронирование через Toka. "
                    "Проверьте данные (дату/время, количество гостей, телефон) и попробуйте ещё раз."
                )
                state["pipeline_trace"] = _trace_append(
                    state,
                    "create_reservation",
                    {"ok": False, "error": str(exc)[:500]},
                )
                return state

        # Build graph
        graph = StateGraph(RecState)
        graph.add_node("extract_requirements", extract_requirements_node)
        graph.add_node("ask_questions", ask_questions_node)
        graph.add_node("confirm_search_plan", confirm_search_plan_node)
        graph.add_node("build_yandex_queries", build_yandex_queries_node)
        graph.add_node("yandex_web_search", yandex_web_search_node)
        graph.add_node("dedupe_and_filter_urls", dedupe_and_filter_urls_node)
        graph.add_node("fetch_afisha_cards", fetch_afisha_cards_node)
        graph.add_node("toka_capacity_gate", toka_capacity_gate_node)
        graph.add_node("formal_rank", formal_rank_node)
        graph.add_node("take_shortlist", take_shortlist_node)
        graph.add_node("relax_fallback", relax_criteria_fallback_node)
        graph.add_node("fetch_and_analyze_reviews", fetch_and_analyze_reviews_shortlist_node)
        graph.add_node("final_rerank_and_explain", final_rerank_and_explain_node)
        graph.add_node("format_reply", format_reply_node)
        graph.add_node("extract_booking_requirements", extract_booking_requirements_node)
        graph.add_node("ask_booking_questions", ask_booking_questions_node)
        graph.add_node("create_reservation", create_reservation_node)

        graph.set_entry_point("extract_requirements")

        def route_after_extract(s: RecState) -> str:
            if s.get("booking_pending"):
                return "extract_booking_requirements"
            if not s.get("requirements_complete"):
                return "ask_questions"
            if not bool(s.get("search_plan_confirmed")):
                return "confirm_search_plan"
            return "build_yandex_queries"

        graph.add_conditional_edges(
            "extract_requirements",
            route_after_extract,
            path_map={
                "extract_booking_requirements": "extract_booking_requirements",
                "ask_questions": "ask_questions",
                "confirm_search_plan": "confirm_search_plan",
                "build_yandex_queries": "build_yandex_queries",
            },
        )
        graph.add_edge("ask_questions", END)
        graph.add_edge("confirm_search_plan", END)
        graph.add_conditional_edges(
            "extract_booking_requirements",
            lambda s: "create_reservation" if s.get("booking_complete") else "ask_booking_questions",
            path_map={
                "create_reservation": "create_reservation",
                "ask_booking_questions": "ask_booking_questions",
            },
        )
        graph.add_edge("ask_booking_questions", END)
        graph.add_edge("create_reservation", END)

        graph.add_edge("build_yandex_queries", "yandex_web_search")
        graph.add_edge("yandex_web_search", "dedupe_and_filter_urls")
        graph.add_edge("dedupe_and_filter_urls", "fetch_afisha_cards")
        graph.add_edge("fetch_afisha_cards", "toka_capacity_gate")
        graph.add_edge("toka_capacity_gate", "formal_rank")

        def route_after_rank(s: RecState) -> str:
            cnt = int(s.get("above_threshold_count") or 0)
            if cnt <= 0:
                # If we hit relax limit, just format reply (empty shortlist)
                if int(s.get("relax_attempts") or 0) >= 2:
                    return "format_reply"
                return "relax_fallback"
            return "take_shortlist"

        graph.add_conditional_edges(
            "formal_rank",
            route_after_rank,
            path_map={"relax_fallback": "relax_fallback", "take_shortlist": "take_shortlist", "format_reply": "format_reply"},
        )

        graph.add_edge("relax_fallback", "build_yandex_queries")

        def route_shortlist_reviews(s: RecState) -> str:
            sl = s.get("shortlist") or []
            if len(sl) <= 1:
                return "final_rerank_and_explain"
            return "fetch_and_analyze_reviews"

        # Replace take_shortlist routing with conditional for reviews.
        graph.add_conditional_edges(
            "take_shortlist",
            route_shortlist_reviews,
            path_map={"final_rerank_and_explain": "final_rerank_and_explain", "fetch_and_analyze_reviews": "fetch_and_analyze_reviews"},
        )

        graph.add_edge("fetch_and_analyze_reviews", "final_rerank_and_explain")
        graph.add_edge("final_rerank_and_explain", "format_reply")
        graph.add_edge("format_reply", END)

        app = graph.compile()

        booking_selected_initial = (
            (graph_state.context.get("booking_selected_candidate") if graph_state.context else {}) or {}
        )
        if booking_selected_override is not None:
            booking_selected_initial = booking_selected_override

        _ctx: Dict[str, Any] = dict(graph_state.context or {})

        def _ctx_candidate_list(key: str) -> List[Dict[str, Any]]:
            v = _ctx.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            return []

        initial_state: RecState = {
            "session_id": session_id,
            "current_node": graph_state.current_node,
            "reply": "",
            "recommendation_requirements": graph_state.context.get("recommendation_requirements") if graph_state.context else {},
            "requirements_complete": False,
            "missing_fields": [],
            "repeated_missing_fields": [],
            "requirements_prompt_count": int(_ctx.get("requirements_prompt_count") or 0),
            "search_plan_confirmed": bool(_ctx.get("search_plan_confirmed")),
            "search_plan_fingerprint": _ctx.get("search_plan_fingerprint")
            if isinstance(_ctx.get("search_plan_fingerprint"), str)
            else None,
            "booking_pending": bool(graph_state.context.get("booking_pending")) if graph_state.context else False,
            "booking_selected_candidate": booking_selected_initial,
            "booking_requirements": (graph_state.context.get("booking_requirements") if graph_state.context else {}) or {},
            "booking_complete": bool(graph_state.context.get("booking_complete")) if graph_state.context else False,
            "booking_missing_fields": (
                graph_state.context.get("booking_missing_fields") if graph_state.context else []
            )
            or [],
            "reservation_result": (graph_state.context.get("reservation_result") if graph_state.context else {}) or {},
            "booking_errors": (graph_state.context.get("booking_errors") if graph_state.context else []) or [],
            "yandex_queries": [],
            "yandex_urls": [],
            "candidates": [],
            "scored_candidates": [],
            "min_score": 0.0,
            "above_threshold_count": 0,
            "shortlist": _ctx_candidate_list("shortlist"),
            "reviews_aspects": [],
            "final_recommendations": _ctx_candidate_list("final_recommendations"),
            "recommendations": _ctx_candidate_list("recommendations"),
            "relax_attempts": int(graph_state.context.get("relax_attempts") or 0) if graph_state.context else 0,
            "service_errors": [],
            # Trace is persisted per turn to pipeline_events; do not reload from context (avoids unbounded JSON).
            "pipeline_trace": [],
        }

        trace_batch_id = str(uuid.uuid4())
        result_state = await app.ainvoke(initial_state)

        reply = str(result_state.get("reply") or "")
        trace_events = list(result_state.get("pipeline_trace") or [])
        await self.state_repository.append_pipeline_events(
            session_id=session_id,
            batch_id=trace_batch_id,
            events=trace_events,
        )

        final_context = result_state.copy()
        _ctx_exclude = {"session_id", "current_node", "reply", "pipeline_trace"}

        await self.state_repository.append_history(
            session_id=session_id,
            messages=messages,
            reply=reply,
        )
        await self.state_repository.update_current_node_and_context(
            session_id=session_id,
            current_node=str(result_state.get("current_node") or "format_reply"),
            context={k: v for k, v in final_context.items() if k not in _ctx_exclude},
        )

        updated_state = await self.state_repository.get_state_for_session(session_id)
        return {"reply": reply, "session_id": session_id, "state": updated_state.to_dict()}


graph_runner_singleton: Optional[GraphRunner] = None


def get_graph_runner() -> GraphRunner:
    # Lazy singleton initialization. In a real app, wire this with FastAPI dependency injection properly.
    global graph_runner_singleton
    if graph_runner_singleton is None:
        from ..storage.database import get_session_maker
        from ..storage.session_store import SessionStore
        from ..storage.state_repository import StateRepository
        from ..services.llm import LLMClientRegistry

        session_maker = get_session_maker()
        session_store = SessionStore(session_maker)
        state_repo = StateRepository(session_maker)
        llm_registry = LLMClientRegistry.from_config()

        graph_runner_singleton = GraphRunner(
            session_store=session_store,
            state_repository=state_repo,
            llm_registry=llm_registry,
        )
    return graph_runner_singleton

