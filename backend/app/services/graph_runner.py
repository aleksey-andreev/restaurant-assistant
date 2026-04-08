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
            prev_req: Dict[str, Any] = dict(state.get("recommendation_requirements") or {})

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
                occ = req.get("occasion")
                if not (isinstance(occ, str) and bool(occ.strip())):
                    out.append("occasion")
                return out

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
                "occasion — только если повод назван явно (в т.ч. «годовщина свадьбы», «день рождения»). Иначе пусто/null.\n\n"
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

            raw = await llm_client.chat(
                messages=[{"role": "system", "content": prompt_sys}, {"role": "user", "content": user_msg}],
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
                return {
                    **state,
                    "current_node": "extract_requirements",
                    "requirements_complete": len(miss) == 0,
                    "missing_fields": miss,
                    "recommendation_requirements": prev_fixed,
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

            return {
                **state,
                "current_node": "extract_requirements",
                "recommendation_requirements": normalized,
                "requirements_complete": requirements_complete,
                "missing_fields": missing_fields,
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
                    },
                ),
            }

        async def ask_questions_node(state: RecState) -> RecState:
            missing = state.get("missing_fields") or [
                "city",
                "party_size",
                "budget_range",
                "location_or_metro",
                "occasion",
            ]
            field_labels: Dict[str, str] = {
                "city": "в каком городе ищем ресторан",
                "city_slug": "уточните название города (как в обычном адресе), чтобы найти рестораны в каталоге Афиши",
                "party_size": "сколько человек будет (число участников)",
                "budget_range": "общий бюджет на компанию в рублях (от … до … на весь визит)",
                "location_or_metro": "район или станция метро, где удобно",
                "occasion": "повод встречи",
            }
            lines = [f"- {field_labels.get(code, code)}" for code in missing]
            reply = (
                "Чтобы подобрать подходящие рестораны, уточните, пожалуйста:\n"
                + "\n".join(lines)
                + "\n\nМожно ответить одним сообщением со всеми пунктами."
            )
            state["reply"] = reply
            state["current_node"] = "ask_questions"
            state["pipeline_trace"] = _trace_append(
                state, "ask_questions", {"missing_fields": missing}
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

            # Map occasion keywords to typical words on Afisha pages
            occasion = str(req.get("occasion") or "").lower()
            occasion_terms = []
            if "день рождения" in occasion or "birthday" in occasion:
                occasion_terms = ["банкет", "банкеты"]
            elif "юбилей" in occasion or "anniversary" in occasion:
                occasion_terms = ["банкеты", "кейтеринг", "банкет"]
            elif "роман" in occasion:
                occasion_terms = ["свидание", "романтическая", "уют"]

            occ_part = " ".join(occasion_terms) if occasion_terms else ""

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

            q1 = " ".join([base, loc_part, cuisine_part, occ_part]).strip()
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
            urls = filter_and_order_afisha_restaurant_urls(list(state.get("yandex_urls") or []))[
                :30
            ]
            for url in urls:
                try:
                    cand = await fetch_and_parse_afisha_card(url)
                    # basic sanity
                    if not cand.get("name") and not cand.get("metro"):
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
                        "names": [c.get("name") for c in candidates[:10]],
                    },
                ),
            }

        async def formal_rank_node(state: RecState) -> RecState:
            from .recommendation_ranker import rank_candidates

            requirements = state.get("recommendation_requirements") or {}
            candidates = state.get("candidates") or []

            ranked = rank_candidates(candidates, requirements)
            sc = ranked.get("scored_candidates") or []
            return {
                **state,
                "current_node": "formal_rank",
                "scored_candidates": sc,
                "min_score": ranked.get("min_score") or 0.0,
                "above_threshold_count": ranked.get("above_threshold_count") or 0,
                "pipeline_trace": _trace_append(
                    state,
                    "formal_rank",
                    {
                        "min_score": ranked.get("min_score"),
                        "above_threshold_count": ranked.get("above_threshold_count"),
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

            req = state.get("recommendation_requirements") or {}
            occasion = str(req.get("occasion") or "")

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
                        occasion=occasion,
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
            req = state.get("recommendation_requirements") or {}
            occasion = str(req.get("occasion") or "").lower()

            shortlist = state.get("shortlist") or []
            reviews_aspects = state.get("reviews_aspects") or []
            aspects_by_url = {a.get("url"): a for a in reviews_aspects if a.get("url")}

            def reviews_bonus(aspects: Optional[Dict[str, Any]]) -> float:
                if not aspects:
                    return 0.0
                # Map occasion to which aspects matter; keep deterministic and small.
                if "роман" in occasion:
                    return 0.2 * float(aspects.get("ambience", {}).get("score") or 0.0) + 0.1 * float(
                        aspects.get("noise", {}).get("score") or 0.0
                    ) + 0.05 * float(aspects.get("food", {}).get("score") or 0.0)
                if (
                    "юбилей" in occasion
                    or "anniversary" in occasion
                    or "годовщин" in occasion
                    or "свадьб" in occasion
                    or "wedding" in occasion
                ):
                    return 0.15 * float(aspects.get("service", {}).get("score") or 0.0) + 0.1 * float(
                        aspects.get("food", {}).get("score") or 0.0
                    ) + 0.05 * float(aspects.get("value", {}).get("score") or 0.0)
                if "день рождения" in occasion or "birthday" in occasion:
                    return 0.15 * float(aspects.get("service", {}).get("score") or 0.0) + 0.1 * float(
                        aspects.get("food", {}).get("score") or 0.0
                    ) + 0.05 * float(aspects.get("ambience", {}).get("score") or 0.0)
                return 0.1 * float(aspects.get("food", {}).get("score") or 0.0)

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
                if item.get("occasion_score") is not None and float(item["occasion_score"]) > 0:
                    reasons.append("подходит под повод (банкет/кейтеринг/формат)");

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
                        "occasion_hint": occasion[:120],
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
            last_user = _last_user_text()
            sys = (
                "Ты извлекаешь параметры для бронирования столика через Toka.\n"
                "Верни ТОЛЬКО JSON без markdown.\n"
                "Поля:\n"
                "- booking_complete: true/false\n"
                "- missing_fields: массив строк (из множества: starts_at, guest_count, guest_name, guest_phone)\n"
                "- booking_requirements: объект со следующими полями:\n"
                "  starts_at (ISO-8601 datetime, например 2026-04-02T19:00:00Z)\n"
                "  duration_minutes (number, по умолчанию 120 если не указано)\n"
                "  guest_name (string)\n"
                "  guest_phone (string)\n"
                "  guest_count (number, >=1)\n"
                "  notes (string, можно пустым)\n"
            )
            user_msg = (
                "СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ (для бронирования):\n"
                f"{last_user}\n\n"
                "Контекст (если был):\n"
                f"{json.dumps(state.get('booking_requirements') or {}, ensure_ascii=False)}"
            )

            raw = await llm_client.chat(
                messages=[{"role": "system", "content": sys}, {"role": "user", "content": user_msg}],
                **node_params,
            )

            try:
                json_text = raw
                start = json_text.find("{")
                end = json_text.rfind("}")
                if start >= 0 and end > start:
                    json_text = json_text[start : end + 1]
                parsed = json.loads(json_text)
            except Exception:
                return {
                    **state,
                    "current_node": "extract_booking_requirements",
                    "booking_pending": True,
                    "booking_complete": False,
                    "booking_missing_fields": ["starts_at", "guest_count", "guest_name", "guest_phone"],
                    "booking_requirements": {},
                    "pipeline_trace": _trace_append(
                        state,
                        "extract_booking_requirements",
                        {"parse_ok": False},
                    ),
                }

            br = parsed.get("booking_requirements") or {}
            if not isinstance(br, dict):
                br = {}

            missing_fields: List[str] = []

            def _is_non_empty_str(v: Any) -> bool:
                return isinstance(v, str) and bool(v.strip())

            def _norm_iso_datetime(v: Any) -> Optional[str]:
                if not isinstance(v, str):
                    return None
                s = v.strip()
                # Accept common ISO patterns with Z or timezone offset.
                # Example: 2026-04-02T19:00:00Z / 2026-04-02T19:00:00+03:00
                if not any(t in s for t in ["T", "-"]):
                    return None
                # Lightweight validation to avoid over-rejecting.
                return s

            starts_at = _norm_iso_datetime(br.get("starts_at"))
            if not starts_at:
                missing_fields.append("starts_at")

            guest_count = br.get("guest_count")
            guest_count_norm: Optional[int] = None
            if isinstance(guest_count, (int, float)) and not isinstance(guest_count, bool):
                if int(guest_count) >= 1:
                    guest_count_norm = int(guest_count)
            if guest_count_norm is None:
                missing_fields.append("guest_count")

            guest_name = br.get("guest_name")
            if not _is_non_empty_str(guest_name):
                missing_fields.append("guest_name")

            guest_phone = br.get("guest_phone")
            if not _is_non_empty_str(guest_phone):
                missing_fields.append("guest_phone")

            duration_minutes = br.get("duration_minutes")
            duration_minutes_norm = 120
            if isinstance(duration_minutes, (int, float)) and not isinstance(duration_minutes, bool):
                dm = int(duration_minutes)
                if dm > 0:
                    duration_minutes_norm = dm

            notes = br.get("notes") or ""
            notes_norm = notes if isinstance(notes, str) else ""

            booking_complete = len(missing_fields) == 0

            normalized_req: Dict[str, Any] = {
                "starts_at": starts_at,
                "duration_minutes": duration_minutes_norm,
                "guest_name": guest_name.strip() if isinstance(guest_name, str) else None,
                "guest_phone": guest_phone.strip() if isinstance(guest_phone, str) else None,
                "guest_count": guest_count_norm,
                "notes": notes_norm,
            }

            return {
                **state,
                "current_node": "extract_booking_requirements",
                "booking_pending": True,
                "booking_complete": booking_complete,
                "booking_missing_fields": missing_fields,
                "booking_requirements": normalized_req,
                "pipeline_trace": _trace_append(
                    state,
                    "extract_booking_requirements",
                    {"parse_ok": True, "booking_complete": booking_complete, "missing": missing_fields},
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
                from ..services.toka_client import (
                    TokaBackofficeClient,
                    TokaClientError,
                    find_table_capacity,
                    get_toka_backoffice_client,
                )

                client: TokaBackofficeClient = await get_toka_backoffice_client()

                def _pick_id(obj: Dict[str, Any]) -> Optional[str]:
                    if not isinstance(obj, dict):
                        return None
                    for k in ("id", "store_id", "storeId", "organization_id", "organizationId"):
                        v = obj.get(k)
                        if v is not None and str(v).strip():
                            return str(v)
                    return None

                organizations = await client.get_my_organizations()
                org_items = list(organizations.get("items") or [])
                if not org_items:
                    raise RuntimeError("Toka: no organizations returned")
                org_id = _pick_id(org_items[0])
                if not org_id:
                    raise RuntimeError("Toka: cannot extract org_id from organizations item")

                stores = await client.list_stores(org_id)
                store_items = list(stores.get("items") or [])
                if not store_items:
                    raise RuntimeError("Toka: no stores returned")
                chosen_store = None
                for st in store_items:
                    if st.get("has_tables") is True:
                        chosen_store = st
                        break
                chosen_store = chosen_store or store_items[0]
                store_id = _pick_id(chosen_store)
                if not store_id:
                    raise RuntimeError("Toka: cannot extract store_id from store item")

                org_id_str = org_id
                store_id_str = store_id

                halls = await client.get_halls_and_tables(org_id_str, store_id_str)

                guest_count = int(req.get("guest_count"))
                # Pick the smallest table that fits guest_count.
                chosen_table_id: Optional[str] = None
                chosen_capacity: Optional[int] = None
                for hall in halls.get("items") or []:
                    for table in hall.get("tables") or []:
                        cap = table.get("capacity")
                        if cap is None:
                            continue
                        try:
                            cap_i = int(cap)
                        except Exception:
                            continue
                        if cap_i < guest_count:
                            continue
                        tid = table.get("id")
                        if tid is None:
                            continue
                        tid_str = str(tid)
                        if chosen_capacity is None or cap_i < chosen_capacity:
                            chosen_capacity = cap_i
                            chosen_table_id = tid_str

                if not chosen_table_id:
                    raise RuntimeError("Toka: no tables with enough capacity for guest_count")

                cap_check = find_table_capacity(halls, chosen_table_id)
                if cap_check is not None and guest_count > cap_check:
                    raise RuntimeError("Toka: chosen table capacity is smaller than guest_count")

                starts_at = str(req.get("starts_at"))
                duration_minutes = int(req.get("duration_minutes") or 120)
                payload = {
                    "table_id": chosen_table_id,
                    "starts_at": starts_at,
                    "duration_minutes": duration_minutes,
                    "guest_name": str(req.get("guest_name") or ""),
                    "guest_phone": str(req.get("guest_phone") or ""),
                    "guest_count": guest_count,
                    "notes": str(req.get("notes") or ""),
                    "source": "agent",
                }

                reservation = await client.create_reservation(org_id_str, store_id_str, payload)
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
                        "store_id": store_id_str,
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
        graph.add_node("build_yandex_queries", build_yandex_queries_node)
        graph.add_node("yandex_web_search", yandex_web_search_node)
        graph.add_node("dedupe_and_filter_urls", dedupe_and_filter_urls_node)
        graph.add_node("fetch_afisha_cards", fetch_afisha_cards_node)
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

        graph.add_conditional_edges(
            "extract_requirements",
            lambda s: "extract_booking_requirements"
            if s.get("booking_pending")
            else ("ask_questions" if not s.get("requirements_complete") else "build_yandex_queries"),
            path_map={
                "extract_booking_requirements": "extract_booking_requirements",
                "ask_questions": "ask_questions",
                "build_yandex_queries": "build_yandex_queries",
            },
        )
        graph.add_edge("ask_questions", END)
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
        graph.add_edge("fetch_afisha_cards", "formal_rank")

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

