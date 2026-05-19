from __future__ import annotations

import asyncio
import logging
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

from ..services.afisha_city_slug import list_supported_city_labels_ru, resolve_afisha_city_slug
from ..services.location_reference import (
    apply_canonical_location_to_req,
    build_collect_requirements_location_hint,
    location_reference_enabled,
    run_elicitation_llm_with_location_tools,
    validate_recommendation_requirements_fields_with_location,
)
from ..services.llm import LLMClientRegistry
from ..storage.afisha_catalog_repository import (
    AfishaCatalogRepository,
    catalog_entry_to_candidate,
)
from ..storage.session_store import SessionStore
from ..storage.state_repository import StateRepository
from .search_plan_short_reply import classify_search_plan_short_reply

logger = logging.getLogger(__name__)

def fingerprint_search_plan(req: Dict[str, Any]) -> str:
    """
    Stable fingerprint for search-relevant requirement fields (confirm / invalidate).
    Branches on intent: 'named_restaurant' uses name/city/hint; 'search' uses city/loc/cuisine.
    """
    intent = (req.get("intent") or "search").strip().lower()
    city = (req.get("city") or "").strip().lower() if isinstance(req.get("city"), str) else ""

    if intent == "named_restaurant":
        name = (req.get("restaurant_name") or "").strip().lower()
        hint = (req.get("address_or_hint") or "").strip().lower()
        payload = {"intent": "named_restaurant", "city": city, "name": name, "hint": hint}
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    # intent == "search"
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
        "intent": "search",
        "city": city,
        "party": party,
        "budget": (mn, mx) if mn is not None and mx is not None else None,
        "loc": loc_key,
        "cw": cw,
        "ca": ca,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def format_search_plan_summary(req: Dict[str, Any], *, include_confirmation_hint: bool = True) -> str:
    """
    Human-readable plan for user confirmation.
    Branches on intent: 'named_restaurant' shows name/city/hint; 'search' shows filters.
    """
    intent = (req.get("intent") or "search").strip().lower()

    if intent == "named_restaurant":
        name = str(req.get("restaurant_name") or "").strip() or "—"
        city = str(req.get("city") or "").strip() or "—"
        hint = str(req.get("address_or_hint") or "").strip()
        lines = [
            "Бронирование конкретного ресторана (проверьте и подтвердите):",
            f"- Ресторан: {name}",
            f"- Город: {city}",
        ]
        if hint:
            lines.append(f"- Ориентир/адрес: {hint}")
        if include_confirmation_hint:
            lines.extend([
                "",
                "Если всё верно — нажмите «Подтвердить» или ответьте согласием в чате. "
                "Чтобы изменить — напишите уточнение.",
            ])
        return "\n".join(lines)

    # intent == "search"
    city = req.get("city") if isinstance(req.get("city"), str) else ""
    city = city.strip() or "—"
    ps = req.get("party_size")
    party_s = f"{int(ps)}" if isinstance(ps, (int, float)) and not isinstance(ps, bool) and int(ps) >= 1 else "не указано"

    br = req.get("budget_range") or {}
    mn = mx = None
    if isinstance(br, dict):
        try:
            mn = float(br.get("min"))
            mx = float(br.get("max"))
        except (TypeError, ValueError):
            pass
    if mn is not None and mx is not None:
        budget_s = f"от {_fmt_money(mn)} до {_fmt_money(mx)} ₽"
    else:
        budget_s = "не указан"

    loc = req.get("location")
    if isinstance(loc, dict) and loc.get("type") in {"metro", "area"}:
        lv = loc.get("value")
        if isinstance(lv, str) and lv.strip():
            loc_s = f"{_location_type_label_ru(loc.get('type'))} {lv.strip()}"
        else:
            loc_s = "весь город"
    elif isinstance(loc, dict) and loc.get("type") == "none":
        loc_s = "весь город"
    else:
        loc_s = "не указана"

    wanted = [str(x).strip() for x in (req.get("cuisine_wanted") or []) if isinstance(x, str) and str(x).strip()]
    avoided = [str(x).strip() for x in (req.get("cuisine_avoid") or []) if isinstance(x, str) and str(x).strip()]
    if wanted:
        cuisine_s = ", ".join(wanted[:6]) + ("…" if len(wanted) > 6 else "")
    elif avoided:
        cuisine_s = f"без ограничений; исключить: {', '.join(avoided[:4])}"
    else:
        cuisine_s = "без ограничений"

    lines = [
        "Параметры поиска (проверьте и подтвердите):",
        f"- Город: {city}",
        f"- Гостей: {party_s}",
        f"- Бюджет: {budget_s}",
        f"- Локация: {loc_s}",
        f"- Кухня: {cuisine_s}",
    ]
    if include_confirmation_hint:
        lines.extend(
            [
                "",
                "Если всё верно — нажмите «Подтвердить» под сообщением или ответьте коротким согласием в чате. "
                "Чтобы изменить параметры — напишите уточнение в чате.",
            ]
        )
    return "\n".join(lines)


# Pipeline / ranking only — never expose in dialog context to the SPA.
_CANDIDATE_INTERNAL_KEYS = frozenset({"toka_capacity_message", "toka_capacity_verified"})

_CANDIDATE_LIST_CONTEXT_KEYS = (
    "final_recommendations",
    "recommendations",
    "shortlist",
)


def strip_candidate_for_client(candidate: Any) -> Any:
    if not isinstance(candidate, dict):
        return candidate
    return {k: v for k, v in candidate.items() if k not in _CANDIDATE_INTERNAL_KEYS}


def sanitize_context_for_client(context: Dict[str, Any]) -> Dict[str, Any]:
    """Remove pipeline-only candidate fields before persisting or returning state to the UI."""
    out = dict(context)
    for key in _CANDIDATE_LIST_CONTEXT_KEYS:
        val = out.get(key)
        if isinstance(val, list):
            out[key] = [strip_candidate_for_client(x) for x in val]
    selected = out.get("booking_selected_candidate")
    if isinstance(selected, dict):
        out["booking_selected_candidate"] = strip_candidate_for_client(selected)
    return out


def _toka_capacity_trace_notes(candidates: List[Dict[str, Any]], *, limit: int = 50) -> List[Dict[str, Any]]:
    notes: List[Dict[str, Any]] = []
    for c in candidates:
        msg = c.get("toka_capacity_message")
        verified = c.get("toka_capacity_verified")
        if not msg and verified is True:
            continue
        notes.append(
            {
                "url": c.get("url"),
                "name": c.get("name"),
                "verified": verified,
                "message": msg,
            }
        )
        if len(notes) >= limit:
            break
    return notes


def attach_city_slug_from_reference(req: Dict[str, Any]) -> Dict[str, Any]:
    """Set city_slug only via reference lookup on canonical city (no slang transliteration)."""
    out = dict(req)
    city = out.get("city")
    if isinstance(city, str) and city.strip():
        out["city_slug"] = resolve_afisha_city_slug(city.strip())
    else:
        out["city_slug"] = None
    return out


_ELICITATION_SLOT_LABELS: Dict[str, str] = {
    "city": "город (официальное полное название на русском из списка поддерживаемых)",
    "restaurant_name": "название ресторана",
    "location_or_cuisine": "район, метро или тип кухни",
}

_COLLECT_LLM_RECOVERY_REPLY = (
    "Не удалось сформировать ответ. Попробуйте переформулировать сообщение."
)


def _dialog_graph_invoke_timeout_s() -> float:
    raw = os.environ.get("DIALOG_GRAPH_TIMEOUT_S", "120")
    try:
        return max(30.0, float(raw))
    except (TypeError, ValueError):
        return 120.0


def validate_recommendation_requirements_fields(req: Dict[str, Any]) -> List[str]:
    """
    Детерминированная проверка полноты критериев поиска.

    Ветка named_restaurant: имя + город (валидный city_slug).
    Ветка search: город + (локация ИЛИ кухня).
    """
    missing: List[str] = []
    intent = req.get("intent") or "search"

    city = req.get("city")
    city_ok = isinstance(city, str) and bool(city.strip())
    slug = req.get("city_slug")
    slug_ok = isinstance(slug, str) and bool(slug.strip())
    if not city_ok or not slug_ok:
        missing.append("city")

    if intent == "named_restaurant":
        name = req.get("restaurant_name")
        if not (isinstance(name, str) and name.strip()):
            missing.append("restaurant_name")
    else:
        loc = req.get("location")
        has_location = isinstance(loc, dict) and loc.get("type") in {"metro", "area", "none"}
        wanted = req.get("cuisine_wanted") or []
        avoided = req.get("cuisine_avoid") or []
        has_cuisine = bool(
            (isinstance(wanted, list) and wanted) or (isinstance(avoided, list) and avoided)
        )
        if not has_location and not has_cuisine:
            missing.append("location_or_cuisine")

    return missing


def compute_elicitation_validation_hints(
    req: Dict[str, Any],
    elicitation_prior: Optional[Dict[str, Any]] = None,
    *,
    districts: Optional[List[Dict[str, str]]] = None,
    metro_names: Optional[List[str]] = None,
) -> tuple[List[str], List[str], List[str]]:
    """missing, unresolved (answered but not accepted), not_yet (not asked last turn)."""
    req_checked = attach_city_slug_from_reference(dict(req))
    slug = str(req_checked.get("city_slug") or "").strip().lower()
    if location_reference_enabled(slug) and (districts is not None or metro_names is not None):
        missing = validate_recommendation_requirements_fields_with_location(
            req_checked,
            districts=districts or [],
            metro_names=metro_names or [],
            base_validate=validate_recommendation_requirements_fields,
        )
    else:
        missing = validate_recommendation_requirements_fields(req_checked)
    prior_slots: set[str] = set()
    if elicitation_prior:
        prior_slots = {
            str(x)
            for x in (elicitation_prior.get("asked_slots") or [])
            if isinstance(x, str) and str(x).strip()
        }
    unresolved = [s for s in missing if s in prior_slots]
    not_yet = [s for s in missing if s not in prior_slots]
    return missing, unresolved, not_yet


def _format_elicitation_slot_labels(slots: List[str]) -> str:
    return ", ".join(_ELICITATION_SLOT_LABELS.get(s, s) for s in slots) or "—"


def build_elicitation_user_reply(
    *,
    missing: List[str],
    unresolved: List[str],
    not_yet: List[str],
    req: Dict[str, Any],
) -> str:
    """Deterministic user-facing question when LLM user_reply is missing or echoed."""
    intent = str(req.get("intent") or "search")
    rname = str(req.get("restaurant_name") or "").strip()

    if "city" in missing:
        if "city" in unresolved:
            return (
                "Не удалось определить город по вашему ответу. "
                "Укажите, пожалуйста, полное название города "
                "(например Москва или Санкт-Петербург)."
            )
        if intent == "named_restaurant" and rname:
            return f"Записал «{rname}». В каком городе этот ресторан?"
        return "В каком городе искать ресторан?"

    if "restaurant_name" in missing:
        return "Как называется ресторан, который хотите забронировать?"

    if "location_or_cuisine" in missing:
        if "location_or_cuisine" in unresolved:
            return (
                "Не удалось понять район, метро или тип кухни. "
                "Уточните, пожалуйста, где или какую кухню предпочитаете."
            )
        return (
            "Уточните, пожалуйста, район или станцию метро, либо тип кухни — "
            "так проще подобрать варианты."
        )

    return ""


def build_elicitation_fallback_user_reply(
    *,
    missing: List[str],
    unresolved: List[str],
    not_yet: List[str],
    req: Dict[str, Any],
) -> str:
    """Backward-compatible alias for build_elicitation_user_reply."""
    return build_elicitation_user_reply(
        missing=missing,
        unresolved=unresolved,
        not_yet=not_yet,
        req=req,
    )


def pick_elicitation_user_reply(
    *,
    parsed: Dict[str, Any],
    last_user_text: str,
    req_complete: bool,
    new_req: Dict[str, Any],
    missing_fb: List[str],
    unresolved_fb: List[str],
    not_yet_fb: List[str],
) -> tuple[str, str, List[str]]:
    """
    LLM wording when user_reply is valid and not an echo; else deterministic templates.
    Returns (reply_text, reply_source, asked_slots).
    """
    llm_reply = str(parsed.get("user_reply") or "").strip()
    llm_ok = bool(llm_reply) and not elicitation_reply_echoes_user_utterance(
        llm_reply, last_user_text
    )

    if req_complete:
        user_reply = build_elicitation_complete_user_reply(new_req)
        reply_source = "template_complete"
        if llm_ok:
            return llm_reply, "llm_ack", []
        return user_reply, reply_source, []

    template_reply = build_elicitation_user_reply(
        missing=missing_fb,
        unresolved=unresolved_fb,
        not_yet=not_yet_fb,
        req=new_req,
    )
    asked_slots_new = [
        s for s in missing_fb if s in unresolved_fb or s in not_yet_fb
    ] or list(missing_fb)
    llm_slots = [str(x) for x in (parsed.get("asked_slots") or []) if isinstance(x, str)]
    if llm_slots:
        asked_slots_new = llm_slots

    if llm_ok:
        return llm_reply, "llm_question", asked_slots_new
    if template_reply:
        return template_reply, "template_question", asked_slots_new
    return _COLLECT_LLM_RECOVERY_REPLY, "recovery", asked_slots_new


def build_elicitation_complete_user_reply(req: Dict[str, Any]) -> str:
    """Acknowledgement when criteria are complete (before search pipeline)."""
    intent = str(req.get("intent") or "search")
    rname = str(req.get("restaurant_name") or "").strip()
    city = str(req.get("city") or "").strip()
    if intent == "named_restaurant" and rname and city:
        return f"Записал «{rname}» в {city}. Начинаю поиск и проверку бронирования."
    if intent == "named_restaurant" and rname:
        return f"Записал «{rname}». Начинаю поиск."
    if city:
        return f"Понял, ищем в {city}. Подбираю варианты."
    return "Критерии понятны — начинаю подбор ресторанов."


def build_elicitation_validation_feedback_block(
    *,
    missing: List[str],
    unresolved: List[str],
    not_yet: List[str],
    elicitation_prior: Dict[str, Any],
    last_user_text: str,
) -> str:
    """Structured validation context for the elicitation LLM."""
    lines = [
        "Состояние проверки (служебно; не цитируй дословно последнюю реплику в user_reply):",
        f"- Не хватает полей: {_format_elicitation_slot_labels(missing) if missing else 'ничего'}",
    ]
    if unresolved:
        lines.append(
            "- Пользователь уже отвечал на твой вопрос про: "
            f"{_format_elicitation_slot_labels(unresolved)}, "
            "но в черновике критериев это ещё не принято "
            "(нет значения или город не из поддерживаемых каталога)."
        )
    if not_yet:
        lines.append(
            "- Про эти поля ты ещё не спрашивал в прошлом ответе ассистента: "
            f"{_format_elicitation_slot_labels(not_yet)}."
        )
    prior_text = str(elicitation_prior.get("text") or "").strip()
    prior_slots = list(elicitation_prior.get("asked_slots") or [])
    if prior_text and prior_slots:
        lines.append(f"- В прошлом ответе ассистента спрашивали: {prior_slots}")
    if last_user_text.strip() and unresolved:
        lines.append(
            "- Последняя реплика пользователя могла относиться к непринятым полям; "
            "извлеки из неё значения для этих слотов (см. историю диалога)."
        )
    return "\n".join(lines) + "\n\n"


_RE_ELICITATION_STRIP_FENCE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def elicitation_reply_echoes_user_utterance(user_reply: str, last_user_message: str) -> bool:
    """True if the model put the user's own text into user_reply (GigaChat / broken JSON)."""
    a = re.sub(r"\s+", " ", (user_reply or "").strip())
    b = re.sub(r"\s+", " ", (last_user_message or "").strip())
    return bool(a) and bool(b) and a.casefold() == b.casefold()


_RE_ELICITATION_THINK_BLOCK = re.compile(
    r"<\s*think\s*>[\s\S]*?<\s*/\s*think\s*>",
    re.IGNORECASE,
)
_RE_ELICITATION_REDACTED_BLOCK = re.compile(
    r"<\s*redacted_thinking\s*>[\s\S]*?<\s*/\s*redacted_thinking\s*>",
    re.IGNORECASE,
)
_RE_ELICITATION_REDACTED_CLOSE = re.compile(r"<\s*/\s*redacted_thinking\s*>", re.IGNORECASE)


def _strip_elicitation_llm_wrapper(raw: Optional[str]) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _RE_ELICITATION_THINK_BLOCK.sub("", text).strip()
    text = _RE_ELICITATION_REDACTED_BLOCK.sub("", text).strip()
    close = _RE_ELICITATION_REDACTED_CLOSE.search(text)
    if close:
        text = text[close.end() :].strip()
    text = _RE_ELICITATION_STRIP_FENCE.sub("", text).strip()
    return text


def parse_elicitation_llm_json(raw: Optional[str]) -> Dict[str, Any]:
    js = _strip_elicitation_llm_wrapper(raw)
    if not js:
        return {}
    st = js.find("{")
    en = js.rfind("}")
    if st >= 0 and en > st:
        try:
            parsed = json.loads(js[st : en + 1])
            if isinstance(parsed, dict):
                return normalize_elicitation_parsed(parsed)
        except json.JSONDecodeError:
            pass
    return {}


def normalize_elicitation_parsed(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Accept common alias keys from reasoning models without changing the public schema."""
    out = dict(parsed)
    if not str(out.get("user_reply") or "").strip():
        for key in ("user_reply", "reply", "message", "assistant_reply", "response"):
            v = out.get(key)
            if isinstance(v, str) and v.strip():
                out["user_reply"] = v.strip()
                break
    return out


def merge_elicitation_llm_dicts(
    primary: Dict[str, Any], secondary: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge two LLM parses; secondary fills gaps, does not wipe non-null primary fields."""
    a = normalize_elicitation_parsed(dict(primary or {}))
    b = normalize_elicitation_parsed(dict(secondary or {}))
    out = dict(a)
    for key, val in b.items():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        if isinstance(val, list) and not val:
            continue
        if key not in out or out.get(key) in (None, "", []):
            out[key] = val
    return out


def elicitation_parsed_for_trace(
    parsed: Dict[str, Any], *, last_user_text: str = ""
) -> Dict[str, Any]:
    """Sanitized LLM JSON for pipeline_events (what the model returned)."""
    p = normalize_elicitation_parsed(dict(parsed or {}))
    keys = (
        "intent",
        "restaurant_name",
        "city",
        "city_slug",
        "location",
        "party_size",
        "asked_slots",
        "cuisine_wanted",
        "cuisine_avoid",
        "user_reply",
    )
    out: Dict[str, Any] = {k: p.get(k) for k in keys if k in p and p.get(k) not in (None, "", [])}
    ur = str(p.get("user_reply") or "").strip()
    if ur:
        out["user_reply_len"] = len(ur)
        if last_user_text.strip():
            out["user_reply_echo"] = elicitation_reply_echoes_user_utterance(ur, last_user_text)
    return out


def merge_elicitation_llm_parse(prev_req: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    parsed = normalize_elicitation_parsed(dict(parsed or {}))
    new_req = dict(prev_req)
    for field in (
        "intent",
        "restaurant_name",
        "address_or_hint",
        "source_url",
        "location",
        "party_size",
        "budget_range",
        "occasion",
    ):
        v = parsed.get(field)
        if v is not None:
            new_req[field] = v
    for list_field in ("cuisine_wanted", "cuisine_avoid", "must_have"):
        v = parsed.get(list_field)
        if isinstance(v, list) and v:
            new_req[list_field] = v
    city_raw = parsed.get("city")
    if isinstance(city_raw, str) and city_raw.strip():
        new_req["city"] = city_raw.strip()
    return attach_city_slug_from_reference(new_req)


def resolver_req_for_named_restaurant(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Input for resolve_specific_restaurant_candidates.

    Alpha flow stores criteria in recommendation_requirements (restaurant_name, city, …);
    legacy flow used specific_restaurant_requirements (name, city_slug, …).
    """
    legacy = dict(state.get("specific_restaurant_requirements") or {})
    if isinstance(legacy.get("name"), str) and legacy.get("name").strip():
        if isinstance(legacy.get("city_slug"), str) and legacy.get("city_slug").strip():
            return legacy

    rec = dict(state.get("recommendation_requirements") or {})
    if (rec.get("intent") or "search") != "named_restaurant":
        if legacy:
            return legacy
        return {}

    name = str(rec.get("restaurant_name") or rec.get("name") or "").strip()
    city = str(rec.get("city") or "").strip()
    city_slug = str(rec.get("city_slug") or "").strip()
    if not city_slug and city:
        city_slug = resolve_afisha_city_slug(city) or ""
    hint = str(rec.get("address_or_hint") or "").strip()
    url = str(rec.get("source_url") or "").strip()
    if not name:
        return {}
    out: Dict[str, Any] = {
        "name": name,
        "city": city or None,
        "city_slug": city_slug,
        "address_or_hint": hint or None,
        "source_url": url or None,
    }
    return out


def _fmt_money(v: float) -> str:
    if abs(v - round(v)) < 0.005:
        return str(int(round(v)))
    return f"{v:.0f}"


def _location_type_label_ru(loc_type: Any) -> str:
    """User-facing label for recommendation_requirements.location.type."""
    if loc_type == "metro":
        return "метро"
    if loc_type == "area":
        return "район"
    return str(loc_type) if loc_type not in (None, "") else "—"


def _contains_any(text: str, parts: List[str]) -> bool:
    t = text.lower()
    return any(p in t for p in parts)


def detect_specific_booking_intent(last_user_text: str) -> bool:
    """
    Conservative heuristic for direct booking intent.
    """
    text = (last_user_text or "").strip().lower()
    if not text:
        return False
    has_booking_word = _contains_any(
        text,
        ["заброни", "бронь", "бронь стол", "резерв", "зарезерв", "столик"],
    )
    if not has_booking_word:
        return False
    has_specific_marker = _contains_any(
        text,
        ["в ресторане", "в ", "ресторан ", "кафе ", "бар ", "\""],
    )
    has_search_marker = _contains_any(
        text,
        ["подбери", "найди варианты", "посоветуй", "по критериям", "лучшие"],
    )
    return has_specific_marker and not has_search_marker


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
    catalog_entries: List[Dict[str, Any]]
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
    search_plan_revision_requested: bool
    repeated_missing_fields: List[str]
    requirements_prompt_count: int
    # analytics: append-only events persisted in graph_state.context
    pipeline_trace: List[Dict[str, Any]]
    # intent: direct booking of a specific restaurant (legacy; kept for booking pipeline)
    booking_intent_mode: Optional[str]
    specific_restaurant_requirements: Dict[str, Any]
    specific_restaurant_missing_fields: List[str]
    specific_restaurant_resolved: bool
    client_time_zone: Optional[str]
    # elicitation phase (alpha): conversational requirements collection
    last_elicitation: Dict[str, Any]   # {text: str, asked_slots: List[str]}
    elicitation_prior_turn: Dict[str, Any]  # last_elicitation snapshot at start of collect (for validate)
    elicitation_turn: int
    # validation context for next collect turn (persisted across HTTP requests)
    missing_globally: List[str]
    unresolved_from_last_question: List[str]
    not_yet_prompted: List[str]
    # preorder after successful reservation (Toka); must stay in RecState so LangGraph persists keys
    preorder_phase: Optional[str]
    preorder_menu_available: bool
    preorder_organization_id: Optional[str]
    preorder_store_id: Optional[str]
    preorder_table_id: Optional[str]
    preorder_guest_count: Any
    preorder_cart_lines: Any
    preorder_order_result: Any


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
    afisha_catalog_repository: Optional[AfishaCatalogRepository] = None

    async def run_dialog(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str],
        client_action: Optional[Dict[str, Any]] = None,
        client_time_zone: Optional[str] = None,
    ) -> Dict[str, Any]:
        session = await self.session_store.get_or_create_session(session_id)
        session_id = session.session_id

        graph_state = await self.state_repository.get_state_for_session(session_id)
        ctx = graph_state.context or {}

        if client_time_zone and isinstance(client_time_zone, str) and client_time_zone.strip():
            tz = client_time_zone.strip()[:128]
            existing_tz = ctx.get("client_time_zone")
            if not (isinstance(existing_tz, str) and existing_tz.strip()):
                await self.state_repository.merge_context_patch(session_id, {"client_time_zone": tz})
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

        if not (
            client_action
            and client_action.get("type") in ("submit_booking", "select_booking_candidate")
        ):
            ph_raw = ctx.get("preorder_phase")
            ph = str(ph_raw).strip() if ph_raw is not None else ""
            if ph in ("offer", "mode_choice", "browsing", "summary"):
                from .preorder_dialog import try_handle_preorder_dialog

                preorder_out = await try_handle_preorder_dialog(
                    session_id=session_id,
                    messages=messages,
                    client_action=client_action,
                    ctx=ctx,
                    state_repository=self.state_repository,
                    llm_registry=self.llm_registry,
                )
                if preorder_out is not None:
                    return preorder_out
            if ph == "done" and ctx.get("save_receipt_offered") and not ctx.get("save_receipt_done"):
                from .receipt_save_dialog import try_handle_receipt_save

                save_out = await try_handle_receipt_save(
                    session_id=session_id,
                    messages=messages,
                    client_action=client_action,
                    ctx=ctx,
                    state_repository=self.state_repository,
                )
                if save_out is not None:
                    return save_out

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
                "table_id": client_action.get("table_id"),
            }

        llm_client, system_prompt, node_params = self.llm_registry.get_default_node()
        catalog_repo = self.afisha_catalog_repository

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
                    "booking_pending": True,
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
                        "search_plan_revision_requested": False,
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
                    "search_plan_revision_requested": False,
                    "pipeline_trace": _trace_append(
                        state,
                        "extract_requirements",
                        {"skipped_llm": True, "reason": "confirm_search_plan", "fingerprint": fp0},
                    ),
                }

            if not client_action and not bool(state.get("booking_pending")):
                req_check = dict(state.get("recommendation_requirements") or {})
                miss_check = _missing_fields_for_req(req_check)
                if not miss_check and not bool(state.get("search_plan_confirmed")):
                    short = classify_search_plan_short_reply(_last_user_text())
                    if short == "affirm":
                        fp_a = fingerprint_search_plan(req_check)
                        return {
                            **state,
                            "current_node": "extract_requirements",
                            "recommendation_requirements": req_check,
                            "requirements_complete": True,
                            "missing_fields": [],
                            "search_plan_confirmed": True,
                            "search_plan_fingerprint": fp_a,
                            "search_plan_revision_requested": False,
                            "pipeline_trace": _trace_append(
                                state,
                                "extract_requirements",
                                {
                                    "skipped_llm": True,
                                    "reason": "short_reply_affirm",
                                    "fingerprint": fp_a,
                                },
                            ),
                        }
                    if short == "reject":
                        return {
                            **state,
                            "current_node": "extract_requirements",
                            "recommendation_requirements": req_check,
                            "requirements_complete": True,
                            "missing_fields": [],
                            "search_plan_confirmed": False,
                            "search_plan_revision_requested": True,
                            "search_plan_fingerprint": state.get("search_plan_fingerprint"),
                            "pipeline_trace": _trace_append(
                                state,
                                "extract_requirements",
                                {"skipped_llm": True, "reason": "short_reply_reject"},
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
                "  city_slug: всегда null — система подставит slug только по каноническому полному названию city.\n"
                "  city: официальное название (Санкт-Петербург, не Питер; Владивосток, не Владик).\n"
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
                    "search_plan_revision_requested": False,
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
                "search_plan_revision_requested": False,
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

        async def ask_search_plan_revision_node(state: RecState) -> RecState:
            req = state.get("recommendation_requirements") or {}
            summary = format_search_plan_summary(
                req if isinstance(req, dict) else {}, include_confirmation_hint=False
            )
            state["reply"] = (
                "Хорошо. Напишите, что изменить в параметрах поиска "
                "(город, число гостей, бюджет, район или метро) — можно одним сообщением.\n\n"
                f"{summary}"
            )
            state["search_plan_revision_requested"] = False
            state["current_node"] = "ask_search_plan_revision"
            state["pipeline_trace"] = _trace_append(state, "ask_search_plan_revision", {})
            return state

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

        async def detect_booking_intent_node(state: RecState) -> RecState:
            if bool(state.get("booking_pending")):
                return {
                    **state,
                    "current_node": "detect_booking_intent",
                    "pipeline_trace": _trace_append(
                        state,
                        "detect_booking_intent",
                        {"intent_mode": state.get("booking_intent_mode") or "booking_pending"},
                    ),
                }

            mode = state.get("booking_intent_mode")
            if mode not in {"specific_restaurant", "search"}:
                mode = "search"
            last_text = _last_user_text()
            if detect_specific_booking_intent(last_text):
                mode = "specific_restaurant"
            elif _contains_any(last_text.lower(), ["подбери", "найди варианты", "посоветуй", "по критериям"]):
                mode = "search"
            elif mode != "specific_restaurant":
                mode = "search"
            return {
                **state,
                "current_node": "detect_booking_intent",
                "booking_intent_mode": mode,
                "pipeline_trace": _trace_append(
                    state,
                    "detect_booking_intent",
                    {"intent_mode": mode},
                ),
            }

        async def extract_specific_restaurant_requirements_node(state: RecState) -> RecState:
            prev = dict(state.get("specific_restaurant_requirements") or {})
            missing_prev = list(state.get("specific_restaurant_missing_fields") or [])

            prompt_sys = (
                "Извлеки параметры брони конкретного ресторана из сообщения пользователя. "
                "Верни только JSON без markdown."
            )
            prompt_user = (
                "Верни JSON формата:\n"
                "{\n"
                '  "name": string|null,\n'
                '  "city": string|null,\n'
                '  "address_or_hint": string|null,\n'
                '  "source_url": string|null\n'
                "}\n\n"
                f"Последнее сообщение пользователя:\n{_last_user_text()}\n\n"
                f"Ранее сохранённые значения: {json.dumps(prev, ensure_ascii=False)}\n"
                "Если поле не указано явно, ставь null."
            )
            parsed: Dict[str, Any] = {}
            try:
                raw = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": prompt_sys},
                        {"role": "user", "content": prompt_user},
                    ],
                    **{**node_params, "response_format": {"type": "json_object"}},
                )
                js = raw
                s = js.find("{")
                e = js.rfind("}")
                if s >= 0 and e > s:
                    js = js[s : e + 1]
                payload = json.loads(js)
                if isinstance(payload, dict):
                    parsed = payload
            except Exception:
                parsed = {}

            name = parsed.get("name")
            city = parsed.get("city")
            hint = parsed.get("address_or_hint")
            url = parsed.get("source_url")
            name_out = name.strip() if isinstance(name, str) and name.strip() else (
                prev.get("name").strip() if isinstance(prev.get("name"), str) and prev.get("name").strip() else None
            )
            city_out = city.strip() if isinstance(city, str) and city.strip() else (
                prev.get("city").strip() if isinstance(prev.get("city"), str) and prev.get("city").strip() else None
            )
            hint_out = hint.strip() if isinstance(hint, str) and hint.strip() else (
                prev.get("address_or_hint").strip()
                if isinstance(prev.get("address_or_hint"), str) and prev.get("address_or_hint").strip()
                else None
            )
            url_out = url.strip() if isinstance(url, str) and url.strip() else (
                prev.get("source_url").strip()
                if isinstance(prev.get("source_url"), str) and prev.get("source_url").strip()
                else None
            )
            city_slug = resolve_afisha_city_slug(city_out)
            req = {
                "name": name_out,
                "city": city_out,
                "city_slug": city_slug,
                "address_or_hint": hint_out,
                "source_url": url_out,
            }
            missing: List[str] = []
            if not name_out:
                missing.append("name")
            if not city_out:
                missing.append("city")

            return {
                **state,
                "current_node": "extract_specific_restaurant",
                "specific_restaurant_requirements": req,
                "specific_restaurant_missing_fields": missing,
                "specific_restaurant_resolved": False,
                "pipeline_trace": _trace_append(
                    state,
                    "extract_specific_restaurant",
                    {"missing_fields": missing, "repeated_missing": [x for x in missing if x in set(missing_prev)]},
                ),
            }

        async def ask_specific_restaurant_questions_node(state: RecState) -> RecState:
            missing = [str(x) for x in (state.get("specific_restaurant_missing_fields") or []) if isinstance(x, str)]
            labels = {
                "name": "название ресторана",
                "city": "город",
            }
            lines = "\n".join(f"- {labels.get(x, x)}" for x in missing) or "- название ресторана\n- город"
            reply = (
                "Для бронирования конкретного ресторана нужно уточнить:\n"
                f"{lines}\n\n"
                "Можно ответить одним сообщением. Если есть неоднозначность, добавьте адрес/район."
            )
            return {
                **state,
                "current_node": "ask_specific_restaurant_questions",
                "reply": reply,
                "pipeline_trace": _trace_append(
                    state,
                    "ask_specific_restaurant_questions",
                    {"missing_fields": missing},
                ),
            }

        async def resolve_specific_restaurant_node(state: RecState) -> RecState:
            from .toka_specific_restaurant_resolver import resolve_specific_restaurant_candidates

            req = resolver_req_for_named_restaurant(state)
            name = str(req.get("name") or "").strip()
            city_slug = str(req.get("city_slug") or "").strip()
            hint = str(req.get("address_or_hint") or "").strip()
            if not name or not city_slug:
                return {
                    **state,
                    "current_node": "resolve_specific_restaurant",
                    "specific_restaurant_resolved": False,
                    "specific_restaurant_requirements": req,
                    "reply": (
                        "Не удалось определить ресторан для брони: нужны название и город. "
                        "Напишите, пожалуйста, название ресторана и город одним сообщением."
                    ),
                    "pipeline_trace": _trace_append(
                        state,
                        "resolve_specific_restaurant",
                        {
                            "status": "invalid_requirements",
                            "name": name or None,
                            "city_slug": city_slug or None,
                        },
                    ),
                }

            resolved = await resolve_specific_restaurant_candidates(
                city_slug=city_slug,
                restaurant_name=name,
                address_hint=hint,
                city_label=str(req.get("city") or "").strip() or city_slug,
                llm_chat=llm_client.chat,
                llm_node_params=dict(node_params),
            )
            status = str(resolved.get("status") or "")
            candidates = [x for x in (resolved.get("candidates") or []) if isinstance(x, dict)]
            selected = resolved.get("selected") if isinstance(resolved.get("selected"), dict) else None
            errors = list(state.get("service_errors") or [])
            errors.extend([str(x) for x in (resolved.get("errors") or []) if str(x).strip()])

            if status == "resolved" and selected is not None:
                return {
                    **state,
                    "current_node": "resolve_specific_restaurant",
                    "reply": "Нашёл нужный ресторан. Заполните форму бронирования ниже и нажмите «Отправить заявку».",
                    "booking_pending": True,
                    "booking_selected_candidate": selected,
                    "booking_complete": False,
                    "booking_missing_fields": ["starts_at", "guest_count", "guest_name", "guest_phone"],
                    "booking_errors": [],
                    "specific_restaurant_resolved": True,
                    "final_recommendations": [selected],
                    "recommendations": [selected],
                    "shortlist": [selected],
                    "service_errors": errors,
                    "pipeline_trace": _trace_append(
                        state,
                        "resolve_specific_restaurant",
                        {"status": status, "candidate_count": 1},
                    ),
                }

            if status == "ambiguous" and candidates:
                return {
                    **state,
                    "current_node": "resolve_specific_restaurant",
                    "reply": (
                        "Нашёл несколько похожих ресторанов. "
                        "Выберите нужный вариант в карточках ниже, и затем заполните форму бронирования."
                    ),
                    "booking_pending": True,
                    "booking_selected_candidate": {},
                    "booking_complete": False,
                    "booking_missing_fields": ["starts_at", "guest_count", "guest_name", "guest_phone"],
                    "booking_errors": [],
                    "specific_restaurant_resolved": False,
                    "final_recommendations": candidates,
                    "recommendations": candidates,
                    "shortlist": candidates,
                    "service_errors": errors,
                    "pipeline_trace": _trace_append(
                        state,
                        "resolve_specific_restaurant",
                        {"status": status, "candidate_count": len(candidates)},
                    ),
                }

            return {
                **state,
                "current_node": "resolve_specific_restaurant",
                "reply": (
                    "Не удалось однозначно найти этот ресторан. "
                    "Уточните, пожалуйста, адрес/район или пришлите ссылку на карточку."
                ),
                "specific_restaurant_resolved": False,
                "service_errors": errors,
                "pipeline_trace": _trace_append(
                    state,
                    "resolve_specific_restaurant",
                    {"status": status or "not_found", "candidate_count": len(candidates)},
                ),
            }

        async def build_yandex_queries_node(state: RecState) -> RecState:
            req = state.get("recommendation_requirements") or {}
            city_slug = (req.get("city_slug") or "").strip() if isinstance(req.get("city_slug"), str) else ""
            city = req.get("city") or ""

            cuisine_terms = []
            for x in (req.get("cuisine_wanted") or [])[:3]:
                if x:
                    cuisine_terms.append(str(x))
            cuisine_part = " ".join(cuisine_terms) if cuisine_terms else ""

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

            # Metro/area are not passed into SERP — geo is applied post-fetch (LLM on address).
            base = f"site:afisha.ru {city_slug}/restaurant"

            q1 = " ".join([base, cuisine_part]).strip()
            q2 = " ".join([base, "Средний чек", cuisine_part]).strip()
            q3 = " ".join([base, "Открыто", cuisine_part]).strip()
            q4 = " ".join([base, "ресторан", cuisine_part]).strip()

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

        async def load_afisha_candidate_urls_node(state: RecState) -> RecState:
            """
            Load only prefetch-ready rows from PostgreSQL catalog.
            """
            req = state.get("recommendation_requirements") or {}
            city_slug = (req.get("city_slug") or "").strip() if isinstance(req.get("city_slug"), str) else ""
            limit = int(os.environ.get("AFISHA_CATALOG_LIST_LIMIT", "8000"))
            urls: List[str] = []
            catalog_entries: List[Dict[str, Any]] = []
            source = "none"
            if catalog_repo is not None and city_slug:
                catalog_entries_all = await asyncio.to_thread(
                    catalog_repo.list_prefetch_ready_rows_for_city, city_slug, limit=limit
                )
                catalog_entries = [
                    r for r in catalog_entries_all if not bool((r or {}).get("venue_closed"))
                ]
                urls = [r["url"] for r in catalog_entries]
                if urls:
                    source = "catalog_prefetch"
            return {
                **state,
                "yandex_queries": [],
                "yandex_urls": urls,
                "catalog_entries": catalog_entries,
                "current_node": "load_afisha_candidate_urls",
                "pipeline_trace": _trace_append(
                    state,
                    "load_afisha_candidate_urls",
                    {
                        "source": source,
                        "url_count": len(urls),
                        "catalog_prefetch_rows": len(catalog_entries),
                        "catalog_closed_filtered": (
                            (len(catalog_entries_all) - len(catalog_entries))
                            if "catalog_entries_all" in locals()
                            else 0
                        ),
                        "city_slug": city_slug,
                    },
                ),
            }

        async def fetch_afisha_cards_node(state: RecState) -> RecState:
            candidates: List[Dict[str, Any]] = []
            catalog_entries = state.get("catalog_entries") or []
            if isinstance(catalog_entries, list):
                for row in catalog_entries:
                    if not isinstance(row, dict):
                        continue
                    if bool(row.get("venue_closed")):
                        continue
                    try:
                        candidates.append(catalog_entry_to_candidate(row))
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
                        "skipped_closed": 0,
                        "names": [c.get("name") for c in candidates[:10]],
                    },
                ),
            }

        async def cuisine_prefilter_node(state: RecState) -> RecState:
            from .recommendation_ranker import prefilter_candidates_by_cuisine

            req = state.get("recommendation_requirements") or {}
            cands = list(state.get("candidates") or [])
            filtered = prefilter_candidates_by_cuisine(cands, req if isinstance(req, dict) else {})
            return {
                **state,
                "current_node": "cuisine_prefilter",
                "candidates": filtered,
                "pipeline_trace": _trace_append(
                    state,
                    "cuisine_prefilter",
                    {"in_count": len(cands), "out_count": len(filtered)},
                ),
            }

        async def geo_gate_node(state: RecState) -> RecState:
            import re

            def _norm_token(v: Any) -> str:
                if not isinstance(v, str):
                    return ""
                return re.sub(r"\s+", " ", v.strip().lower())

            async def _normalize_area_from_user(req_loc: Dict[str, Any], city_slug: str) -> Optional[str]:
                from .location_reference import search_districts

                raw = req_loc.get("value")
                if not isinstance(raw, str) or not raw.strip():
                    return None
                rows = []
                if catalog_repo is not None and city_slug:
                    rows = await asyncio.to_thread(catalog_repo.list_city_districts, city_slug)
                if rows:
                    hits = search_districts(rows, raw.strip(), limit=3)
                    if len(hits) == 1 and hits[0]["score"] >= 0.78:
                        return hits[0]["district_label"]
                    if hits and hits[0]["score"] >= 0.92:
                        return hits[0]["district_label"]
                if not rows:
                    return None
                norm_to_label: Dict[str, str] = {}
                for r in rows:
                    n = _norm_token(r.get("district_norm"))
                    if n:
                        norm_to_label[n] = str(r.get("district_label") or "").strip()
                options = sorted(norm_to_label.keys())
                prompt = (
                    "Нормализуй пользовательский район к одному из допустимых значений.\n"
                    f"Ввод: {raw.strip()}\n"
                    f"Допустимые значения: {options}\n"
                    "Ответ только JSON вида {\"district_norm\": string|null}."
                )
                try:
                    raw_llm = await llm_client.chat(
                        [{"role": "user", "content": prompt}],
                        **dict(node_params),
                    )
                    s = (raw_llm or "").strip()
                    st = s.find("{")
                    en = s.rfind("}")
                    if st < 0 or en <= st:
                        return None
                    payload = json.loads(s[st : en + 1])
                    chosen = _norm_token(payload.get("district_norm")) if isinstance(payload, dict) else ""
                    if chosen in norm_to_label:
                        return norm_to_label[chosen]
                except Exception:
                    return None
                return None

            cands = list(state.get("candidates") or [])
            req = state.get("recommendation_requirements") or {}
            loc = req.get("location") if isinstance(req, dict) else {}
            city_slug = str(req.get("city_slug") or "").strip() if isinstance(req, dict) else ""
            if not isinstance(loc, dict):
                loc = {}
            loc_t = loc.get("type")
            loc_v = loc.get("value")

            if loc_t not in {"metro", "area"} or not isinstance(loc_v, str) or not loc_v.strip():
                kept = []
                for c in cands:
                    d = dict(c)
                    d["llm_geo_result"] = "match"
                    d["geo_location_score"] = 1.0
                    kept.append(d)
                return {
                    **state,
                    "current_node": "geo_gate",
                    "candidates": kept,
                    "pipeline_trace": _trace_append(
                        state,
                        "geo_gate",
                        {"in_count": len(cands), "out_count": len(kept), "dropped_geo_mismatch": 0},
                    ),
                }

            want_norm = _norm_token(loc_v)
            want_area_label = None
            if loc_t == "area":
                want_area_label = await _normalize_area_from_user(loc, city_slug)
                if not want_area_label:
                    kept = []
                    return {
                        **state,
                        "current_node": "geo_gate",
                        "candidates": kept,
                        "pipeline_trace": _trace_append(
                            state,
                            "geo_gate",
                            {
                                "in_count": len(cands),
                                "out_count": 0,
                                "dropped_geo_mismatch": len(cands),
                                "user_area_unresolved": str(loc_v),
                            },
                        ),
                    }

            kept = []
            dropped = 0
            for c in cands:
                d = dict(c)
                ok = False
                if loc_t == "area":
                    area_norm = _norm_token(d.get("geo_inferred_area"))
                    ok = area_norm == _norm_token(want_area_label)
                elif loc_t == "metro":
                    metros = d.get("geo_osm_metros")
                    metro_norms = (
                        {_norm_token(x) for x in metros if isinstance(x, str) and x.strip()}
                        if isinstance(metros, list)
                        else set()
                    )
                    ok = want_norm in metro_norms
                if ok:
                    d["llm_geo_result"] = "match"
                    d["geo_location_score"] = 1.0
                    kept.append(d)
                else:
                    dropped += 1
            return {
                **state,
                "current_node": "geo_gate",
                "candidates": kept,
                "pipeline_trace": _trace_append(
                    state,
                    "geo_gate",
                    {
                        "in_count": len(cands),
                        "out_count": len(kept),
                        "dropped_geo_mismatch": dropped,
                        "location_type": loc_t,
                        "location_value": loc_v,
                        "normalized_area": want_area_label,
                    },
                ),
            }

        async def external_rating_node(state: RecState) -> RecState:
            from .external_rating import (
                catalog_aggregate_rating_score,
                enrich_candidate_external_rating,
                external_rating_use_yandex,
                stored_yandex_rating_from_catalog,
            )
            from .yandex_web_search import YandexWebSearchClient

            req = state.get("recommendation_requirements") or {}
            city = str(req.get("city") or "").strip()
            max_n = int(os.environ.get("EXTERNAL_RATING_MAX_CANDIDATES", "20"))
            all_cands = list(state.get("candidates") or [])
            head, tail = all_cands[:max_n], all_cands[max_n:]
            errors = list(state.get("service_errors") or [])
            use_yandex_serp = external_rating_use_yandex()
            rating_enabled = os.environ.get("EXTERNAL_RATING_SCORING_ENABLED", "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }

            if not rating_enabled:
                out_disabled: List[Dict[str, Any]] = []
                for c in all_cands:
                    d = dict(c)
                    d["external_rating"] = None
                    d["external_rating_confidence"] = 0.0
                    out_disabled.append(d)
                return {
                    **state,
                    "current_node": "external_rating",
                    "candidates": out_disabled,
                    "service_errors": errors,
                    "pipeline_trace": _trace_append(
                        state,
                        "external_rating",
                        {
                            "candidate_count": len(out_disabled),
                            "rating_enabled": False,
                            "yandex_called": False,
                            "catalog_rating_read": False,
                        },
                    ),
                }

            def _apply_stored_ratings(cc: Dict[str, Any]) -> bool:
                yr, yc = stored_yandex_rating_from_catalog(cc)
                if yr is not None and yc >= 0.45:
                    cc["external_rating"] = yr
                    cc["external_rating_confidence"] = yc
                    return True
                cr, cconf = catalog_aggregate_rating_score(cc.get("card_extras"))
                if cr is not None and cconf >= 0.45:
                    cc["external_rating"] = cr
                    cc["external_rating_confidence"] = cconf
                    return True
                return False

            try:
                y_client = YandexWebSearchClient.from_env()
            except Exception as exc:
                errors.append(str(exc))
                out: List[Dict[str, Any]] = []
                for c in all_cands:
                    d = dict(c)
                    if not _apply_stored_ratings(d):
                        d.setdefault("external_rating", None)
                        d.setdefault("external_rating_confidence", 0.0)
                    out.append(d)
                return {
                    **state,
                    "current_node": "external_rating",
                    "candidates": out,
                    "service_errors": errors,
                    "pipeline_trace": _trace_append(
                        state,
                        "external_rating",
                        {"error": str(exc), "candidate_count": len(out)},
                    ),
                }

            sem = asyncio.Semaphore(3)

            async def rate_one(c: Dict[str, Any]) -> Dict[str, Any]:
                cc = dict(c)
                if _apply_stored_ratings(cc):
                    return cc
                if not use_yandex_serp:
                    cc["external_rating"] = None
                    cc["external_rating_confidence"] = 0.0
                    return cc
                async with sem:
                    rating, conf = await enrich_candidate_external_rating(
                        y_client,
                        restaurant_name=str(cc.get("name") or ""),
                        city=city,
                        address=str(cc.get("address") or "").strip() or None,
                    )
                cc["external_rating"] = rating
                cc["external_rating_confidence"] = conf
                return cc

            rated_head = await asyncio.gather(*[rate_one(dict(x)) for x in head])
            tail_out: List[Dict[str, Any]] = []
            for c in tail:
                d = dict(c)
                if not _apply_stored_ratings(d):
                    d.setdefault("external_rating", None)
                    d.setdefault("external_rating_confidence", 0.0)
                tail_out.append(d)
            merged = list(rated_head) + tail_out
            return {
                **state,
                "current_node": "external_rating",
                "candidates": merged,
                "service_errors": errors,
                "pipeline_trace": _trace_append(
                    state,
                    "external_rating",
                    {"candidate_count": len(merged), "rated_head": len(rated_head)},
                ),
            }

        async def toka_capacity_gate_node(state: RecState) -> RecState:
            from .toka_candidate_capacity import apply_toka_capacity_gate

            req = state.get("recommendation_requirements") or {}
            ps = req.get("party_size")

            candidates = list(state.get("candidates") or [])

            # Если party_size не задан — пропускаем Toka, все кандидаты проходят.
            # Помечаем как verified=True, чтобы apply_toka_unverified_score_penalty
            # не занижал формальный скор.
            party_n: Optional[int] = None
            try:
                if ps is not None and not isinstance(ps, bool):
                    v = int(ps)
                    if v >= 1:
                        party_n = v
            except Exception:
                pass

            if party_n is None:
                gated = [{**c, "toka_capacity_verified": True, "toka_capacity_message": None} for c in candidates]
                return {
                    **state,
                    "current_node": "toka_capacity_gate",
                    "candidates": gated,
                    "pipeline_trace": _trace_append(
                        state,
                        "toka_capacity_gate",
                        {
                            "in_count": len(candidates),
                            "out_count": len(gated),
                            "skipped": True,
                            "reason": "party_size not set — gate skipped",
                        },
                    ),
                }

            gated, extra_err = await apply_toka_capacity_gate(candidates, party_n)
            errors = list(state.get("service_errors") or [])
            errors.extend(extra_err)
            cap_notes = _toka_capacity_trace_notes(gated)

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
                        **({"capacity_notes": cap_notes} if cap_notes else {}),
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

            # First relax: keep metro/area; second relax: city-wide discovery.
            if relax_attempts >= 1:
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
                tid_fb = fb.get("table_id")
                table_id_norm: Optional[str] = None
                if isinstance(tid_fb, str) and tid_fb.strip():
                    table_id_norm = tid_fb.strip()
                booking_complete_fb = len(missing_fields_fb) == 0
                normalized_fb: Dict[str, Any] = {
                    "starts_at": starts_at,
                    "duration_minutes": 120,
                    "guest_name": gn_fb.strip() if isinstance(gn_fb, str) else None,
                    "guest_phone": gp_fb.strip() if isinstance(gp_fb, str) else None,
                    "guest_count": guest_count_norm_fb,
                    "notes": "",
                    "table_id": table_id_norm,
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
                from ..services.toka_gateway import get_toka_gateway, TokaGatewayError

                gateway = await get_toka_gateway()
                starts_at = str(req.get("starts_at"))
                guest_count = int(req.get("guest_count"))
                tid_req = req.get("table_id")
                table_id_kw: Optional[str] = None
                if isinstance(tid_req, str) and tid_req.strip():
                    table_id_kw = tid_req.strip()
                ctz_raw = state.get("client_time_zone")
                ctz: Optional[str] = None
                if isinstance(ctz_raw, str) and ctz_raw.strip():
                    ctz = ctz_raw.strip()[:128]
                reservation_data = await gateway.create_reservation(
                    restaurant_ref=dict(state.get("booking_selected_candidate") or {}),
                    starts_at=starts_at,
                    duration_minutes=int(req.get("duration_minutes") or 120),
                    guest_name=str(req.get("guest_name") or ""),
                    guest_phone=str(req.get("guest_phone") or ""),
                    guest_count=guest_count,
                    notes=str(req.get("notes") or ""),
                    table_id=table_id_kw,
                    client_time_zone=ctz,
                )
                raw = reservation_data.get("raw")
                reservation: Dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
                for k in (
                    "starts_at",
                    "guest_count",
                    "guest_name",
                    "guest_phone",
                    "table_id",
                    "table_title",
                    "restaurant_name",
                    "restaurant_address",
                ):
                    v = reservation_data.get(k)
                    if v is not None and v != "":
                        reservation[k] = v
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
                tt = reservation.get("table_title")
                table_part = f" Стол: {tt}." if isinstance(tt, str) and tt.strip() else ""
                state["reply"] = (
                    "Готово — бронирование создано. "
                    f"Ресторан: {state.get('booking_selected_candidate', {}).get('name') or '—'}. "
                    f"Дата/время: {starts_at}. "
                    f"Гостей: {guest_count}.{table_part}{rid_part}"
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
                resolved = reservation_data.get("resolved") or {}
                oid = str(resolved.get("organization_id") or "").strip()
                sid = str(resolved.get("store_id") or "").strip()
                if oid and sid:
                    try:
                        from .preorder_service import menu_tree_has_positions

                        mtree = await gateway.get_menu_tree(oid, sid)
                        if menu_tree_has_positions(mtree):
                            tid_pre = str(reservation.get("table_id") or table_id_str or "").strip()
                            state["preorder_phase"] = "offer"
                            state["preorder_menu_available"] = True
                            state["preorder_organization_id"] = oid
                            state["preorder_store_id"] = sid
                            state["preorder_table_id"] = tid_pre
                            state["preorder_guest_count"] = int(guest_count)
                            state["reply"] = (
                                str(state.get("reply") or "")
                                + "\n\nХотите оформить предзаказ к этому столу? "
                                "Напишите «да», «ок» — или нажмите кнопку «Оформить предзаказ»."
                            )
                    except Exception:
                        logger.debug("preorder menu availability probe failed", exc_info=True)
                return state
            except Exception as exc:
                gc_raw = req.get("guest_count")
                try:
                    gc_i = int(gc_raw) if gc_raw is not None else None
                except Exception:
                    gc_i = None

                technical_codes = {"TOKA_API_ERROR", "TOKA_UNKNOWN_ERROR", "RESOLVER_NOT_CONFIGURED"}

                if isinstance(exc, TokaGatewayError):
                    code = str(getattr(exc, "code", "") or "")
                    explicit_table = bool(
                        isinstance(req.get("table_id"), str) and str(req.get("table_id")).strip()
                    )
                    if code in technical_codes:
                        logger.exception("Toka reservation failed (technical): code=%s", code)
                        state["reply"] = "Произошла техническая ошибка. Попробуйте позже."
                        state["booking_errors"] = []
                    elif code == "NO_TABLE_AVAILABLE":
                        if explicit_table:
                            state["booking_errors"] = [
                                "Выбранный стол занят в указанное время. "
                                "Выберите другое время или дату, либо другой стол."
                            ]
                            state["reply"] = "Не удалось подтвердить бронь — см. сообщение в форме ниже."
                        else:
                            state["booking_errors"] = []
                            g = f" на {gc_i} гостей" if gc_i and gc_i >= 1 else ""
                            state["reply"] = (
                                "На выбранное время нет свободного столика"
                                f"{g}. Попробуйте изменить дату/время или количество гостей."
                            )
                    elif code == "TABLE_NOT_FOUND":
                        if explicit_table:
                            state["booking_errors"] = [
                                "Стол не найден в системе бронирования. Выберите другой стол или режим «Любой»."
                            ]
                            state["reply"] = "Не удалось подтвердить бронь — см. сообщение в форме ниже."
                        else:
                            state["booking_errors"] = []
                            state["reply"] = "Не удалось найти столик в системе бронирования. Попробуйте изменить параметры."
                    else:
                        state["booking_errors"] = []
                        state["reply"] = "Не удалось создать бронирование. Попробуйте ещё раз."
                else:
                    logger.exception("Reservation failed (unexpected exception)")
                    state["reply"] = "Произошла техническая ошибка. Попробуйте позже."
                    state["booking_errors"] = []

                # В UI: booking_errors показываются в форме; технические — только в чате.
                state["booking_pending"] = True
                state["booking_complete"] = False
                state["current_node"] = "create_reservation_error"
                state["pipeline_trace"] = _trace_append(
                    state,
                    "create_reservation",
                    {"ok": False, "code": getattr(exc, "code", None), "error": str(exc)[:500]},
                )
                return state

        # ── Alpha: новые узлы сбора и валидации требований ────────────────

        req_llm_client, _req_sys, req_node_params = self.llm_registry.get_node_or_default(
            "requirements_elicitation"
        )

        async def collect_requirements_node(state: RecState) -> RecState:
            """
            Один диалоговый ход: LLM-вызов для выявления критериев поиска.
            Обрабатывает confirm_search_plan action и короткие ответы «да/нет»
            без LLM. Возвращает обновлённый черновик требований + текст пользователю.
            """
            # Ранний выход для form_booking_payload — collect пропускаем
            if form_booking_payload is not None:
                return {
                    **state,
                    "current_node": "collect_requirements",
                    "elicitation_prior_turn": dict(state.get("last_elicitation") or {}),
                    "pipeline_trace": _trace_append(
                        state, "collect_requirements", {"skipped": True, "reason": "submit_booking"}
                    ),
                }

            prev_req: Dict[str, Any] = dict(state.get("recommendation_requirements") or {})
            elicitation_prior_turn: Dict[str, Any] = dict(state.get("last_elicitation") or {})

            # Обработка confirm_search_plan action (кнопка «Подтвердить»)
            if client_action and client_action.get("type") == "confirm_search_plan":
                prev_req = attach_city_slug_from_reference(prev_req)
                miss = validate_recommendation_requirements_fields(prev_req)
                if not miss:
                    fp = fingerprint_search_plan(prev_req)
                    spec_sync = resolver_req_for_named_restaurant(
                        {**state, "recommendation_requirements": prev_req}
                    )
                    return {
                        **state,
                        "current_node": "collect_requirements",
                        "elicitation_prior_turn": elicitation_prior_turn,
                        "recommendation_requirements": prev_req,
                        "specific_restaurant_requirements": spec_sync
                        if spec_sync
                        else dict(state.get("specific_restaurant_requirements") or {}),
                        "search_plan_confirmed": True,
                        "search_plan_fingerprint": fp,
                        "search_plan_revision_requested": False,
                        "missing_globally": [],
                        "pipeline_trace": _trace_append(
                            state, "collect_requirements",
                            {"skipped_llm": True, "reason": "confirm_search_plan", "fingerprint": fp}
                        ),
                    }
                # Если поля неполные — продолжаем сбор
                return {
                    **state,
                    "current_node": "collect_requirements",
                    "elicitation_prior_turn": elicitation_prior_turn,
                    "missing_globally": miss,
                    "search_plan_confirmed": False,
                    "pipeline_trace": _trace_append(
                        state, "collect_requirements",
                        {"confirm_rejected": True, "missing": miss}
                    ),
                }

            # Обработка коротких ответов «да/нет» на confirm
            if not client_action and not bool(state.get("search_plan_confirmed")):
                prev_req_slug = attach_city_slug_from_reference(prev_req)
                miss_check = validate_recommendation_requirements_fields(prev_req_slug)
                if not miss_check:
                    short = classify_search_plan_short_reply(_last_user_text())
                    if short == "affirm":
                        fp_a = fingerprint_search_plan(prev_req_slug)
                        return {
                            **state,
                            "current_node": "collect_requirements",
                            "elicitation_prior_turn": elicitation_prior_turn,
                            "recommendation_requirements": prev_req_slug,
                            "search_plan_confirmed": True,
                            "search_plan_fingerprint": fp_a,
                            "search_plan_revision_requested": False,
                            "missing_globally": [],
                            "pipeline_trace": _trace_append(
                                state, "collect_requirements",
                                {"skipped_llm": True, "reason": "short_reply_affirm"}
                            ),
                        }
                    if short == "reject":
                        return {
                            **state,
                            "current_node": "collect_requirements",
                            "elicitation_prior_turn": elicitation_prior_turn,
                            "search_plan_confirmed": False,
                            "search_plan_revision_requested": True,
                            "missing_globally": [],
                            "reply": (
                                "Хорошо, что хотите изменить? Напишите уточнение — "
                                "город, гостей, бюджет, район или кухню."
                            ),
                            "pipeline_trace": _trace_append(
                                state, "collect_requirements",
                                {"skipped_llm": True, "reason": "short_reply_reject"}
                            ),
                        }

            # Сброс флага revision (уже обработан в следующем ходе)
            if bool(state.get("search_plan_revision_requested")):
                prev_req.pop("city_slug", None)  # пересчитаем после изменений

            turn = int(state.get("elicitation_turn") or 0)
            last_user_text = _last_user_text()
            prev_req_for_ref = attach_city_slug_from_reference(dict(prev_req))
            ref_city_slug = str(prev_req_for_ref.get("city_slug") or "").strip().lower()
            ref_districts: List[Dict[str, str]] = []
            ref_metros: List[str] = []
            if location_reference_enabled(ref_city_slug) and catalog_repo is not None:
                ref_districts = await asyncio.to_thread(
                    catalog_repo.list_city_districts, ref_city_slug
                )
                ref_metros = await asyncio.to_thread(
                    catalog_repo.list_distinct_metro_names, ref_city_slug
                )

            missing_globally, unresolved, not_yet = compute_elicitation_validation_hints(
                prev_req,
                elicitation_prior_turn,
                districts=ref_districts if ref_districts else None,
                metro_names=ref_metros if ref_metros else None,
            )

            supported_cities = ", ".join(list_supported_city_labels_ru())
            validation_feedback = build_elicitation_validation_feedback_block(
                missing=missing_globally,
                unresolved=unresolved,
                not_yet=not_yet,
                elicitation_prior=elicitation_prior_turn,
                last_user_text=last_user_text,
            )

            prompt_sys = (
                "Ты извлекаешь критерии подбора/бронирования ресторана из диалога и "
                "кратко отвечаешь пользователю (1–2 предложения).\n\n"
                "Не выводи chain-of-thought и пояснения вне JSON. Ответ — один JSON-объект.\n\n"
                "Схема:\n"
                "{\n"
                '  "intent": "search" | "named_restaurant",\n'
                '  "restaurant_name": string | null,\n'
                '  "city": string | null,\n'
                '  "city_slug": null,\n'
                '  "address_or_hint": string | null,\n'
                '  "source_url": string | null,\n'
                '  "location": {"type": "metro"|"area"|"none", "value": string|null} | null,\n'
                '  "party_size": number | null,\n'
                '  "budget_range": {"min": number, "max": number} | null,\n'
                '  "cuisine_wanted": [string],\n'
                '  "cuisine_avoid": [string],\n'
                '  "must_have": [string],\n'
                '  "occasion": string | null,\n'
                '  "user_reply": string,\n'
                '  "asked_slots": [string]\n'
                "}\n\n"
                "Правила:\n"
                "- Не домысливай party_size, бюджет, кухню, район — только если пользователь сказал.\n"
                "- city — официальное полное название на русском; жаргон нормализуй "
                "(Питер → Санкт-Петербург, Мск → Москва).\n"
                "- city_slug всегда null (slug подставит система).\n"
                f"- Поддерживаемые города: {supported_cities}. "
                "Если город не из списка — city null.\n"
                "- intent='named_restaurant' если назван конкретный ресторан.\n"
                "- user_reply — твой ответ ассистентом; не копируй дословно последнее сообщение "
                "пользователя. Если критериев не хватает — вежливо уточни недостающее.\n"
                "- asked_slots — поля, о которых спрашиваешь в user_reply: "
                "['city', 'restaurant_name', 'location_or_cuisine']; иначе [].\n\n"
                "Пример. «Забронируй ресторан Ипполит» →\n"
                '{"intent":"named_restaurant","restaurant_name":"Ипполит","city":null,'
                '"city_slug":null,"user_reply":"Понял, Ипполит. В каком городе он?",'
                '"asked_slots":["city"]}\n\n'
                "Пример. «Итальянская кухня в центре Питера на 4 человек» →\n"
                '{"intent":"search","city":"Санкт-Петербург","location":{"type":"area",'
                '"value":"центр"},"party_size":4,"cuisine_wanted":["итальянская"],'
                '"user_reply":"Ищу итальянские рестораны в центре Петербурга на четверых.",'
                '"asked_slots":[]}'
            )
            prompt_sys += build_collect_requirements_location_hint(ref_city_slug)

            user_msgs: List[Dict[str, Any]] = [
                m for m in messages if m.get("role") in {"user", "assistant"} and m.get("content")
            ]
            history_text = "\n".join(
                f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content']}"
                for m in user_msgs[-10:]
            )

            prompt_user = (
                f"Ход диалога: {turn + 1}.\n\n"
                f"{validation_feedback}"
                f"История диалога:\n{history_text}\n\n"
                f"Ранее извлечённые критерии (сохраняй, не теряй):\n"
                f"{json.dumps(prev_req, ensure_ascii=False)}\n\n"
                "Обнови критерии и user_reply в JSON по истории и блоку проверки."
            )

            llm_kwargs = {**req_node_params, "response_format": {"type": "json_object"}}
            location_tool_trace: List[Dict[str, Any]] = []

            async def _elicitation_chat(user_content: str) -> tuple[Dict[str, Any], bool, bool]:
                try:
                    if location_reference_enabled(ref_city_slug) and ref_districts:
                        parsed_out, location_tool_trace[:] = (
                            await run_elicitation_llm_with_location_tools(
                                req_llm_client,
                                system_prompt=prompt_sys,
                                user_prompt=user_content,
                                node_params=req_node_params,
                                city_slug=ref_city_slug,
                                districts=ref_districts,
                                metro_names=ref_metros,
                                parse_json=parse_elicitation_llm_json,
                            )
                        )
                        return parsed_out, bool(parsed_out), False
                    raw = await req_llm_client.chat(
                        messages=[
                            {"role": "system", "content": prompt_sys},
                            {"role": "user", "content": user_content},
                        ],
                        **llm_kwargs,
                    )
                    parsed_out = parse_elicitation_llm_json(raw)
                    return parsed_out, bool(parsed_out), False
                except Exception as exc:
                    if "timeout" in type(exc).__name__.lower():
                        logger.warning(
                            "collect_requirements LLM timed out (provider HTTP timeout): %s",
                            exc,
                        )
                        return {}, False, True
                    logger.exception("collect_requirements LLM call failed")
                    return {}, False, False

            parsed_first, parse_ok_first, llm_timed_out = await _elicitation_chat(prompt_user)
            parsed = dict(parsed_first)
            llm_retried = False

            def _req_complete(req: Dict[str, Any]) -> bool:
                r = attach_city_slug_from_reference(dict(req))
                if location_reference_enabled(str(r.get("city_slug") or "")):
                    miss = validate_recommendation_requirements_fields_with_location(
                        r,
                        districts=ref_districts,
                        metro_names=ref_metros,
                        base_validate=validate_recommendation_requirements_fields,
                    )
                else:
                    miss = validate_recommendation_requirements_fields(r)
                return len(miss) == 0

            if (not llm_timed_out) and not parse_ok_first:
                llm_retried = True
                retry_content = (
                    f"{prompt_user}\n\n"
                    "Предыдущий ответ не содержал валидного JSON. "
                    "Верни снова один JSON-объект по схеме в system (критерии + user_reply)."
                )
                parsed_retry, parse_ok_retry, _ = await _elicitation_chat(retry_content)
                if parse_ok_retry or parsed_retry:
                    parsed = merge_elicitation_llm_dicts(parsed_first, parsed_retry)

            new_req = attach_city_slug_from_reference(merge_elicitation_llm_parse(prev_req, parsed))
            loc_meta: Dict[str, Any] = {}
            slug_after = str(new_req.get("city_slug") or "").strip().lower()
            if location_reference_enabled(slug_after) and catalog_repo is not None:
                if slug_after != ref_city_slug or not ref_districts:
                    ref_districts = await asyncio.to_thread(
                        catalog_repo.list_city_districts, slug_after
                    )
                    ref_metros = await asyncio.to_thread(
                        catalog_repo.list_distinct_metro_names, slug_after
                    )
                if ref_districts:
                    new_req, loc_meta = apply_canonical_location_to_req(
                        new_req,
                        districts=ref_districts,
                        metro_names=ref_metros,
                    )
            req_complete = _req_complete(new_req)

            missing_fb, unresolved_fb, not_yet_fb = compute_elicitation_validation_hints(
                new_req,
                elicitation_prior_turn,
                districts=ref_districts if ref_districts else None,
                metro_names=ref_metros if ref_metros else None,
            )

            user_reply, reply_source, asked_slots_new = pick_elicitation_user_reply(
                parsed=parsed,
                last_user_text=last_user_text,
                req_complete=req_complete,
                new_req=new_req,
                missing_fb=missing_fb,
                unresolved_fb=unresolved_fb,
                not_yet_fb=not_yet_fb,
            )

            spec_sync = resolver_req_for_named_restaurant(
                {**state, "recommendation_requirements": new_req}
            )

            return {
                **state,
                "current_node": "collect_requirements",
                "elicitation_prior_turn": elicitation_prior_turn,
                "recommendation_requirements": new_req,
                "specific_restaurant_requirements": spec_sync
                if spec_sync
                else state.get("specific_restaurant_requirements") or {},
                "reply": user_reply,
                "last_elicitation": {"text": user_reply, "asked_slots": asked_slots_new},
                "elicitation_turn": turn + 1,
                "missing_globally": [],
                "unresolved_from_last_question": [],
                "not_yet_prompted": [],
                "pipeline_trace": _trace_append(
                    state,
                    "collect_requirements",
                    {
                        "turn": turn + 1,
                        "asked_slots": asked_slots_new,
                        "intent": new_req.get("intent"),
                        "validation_hints": {
                            "missing": missing_globally,
                            "unresolved": unresolved,
                            "not_yet": not_yet,
                        },
                        "llm_retry": llm_retried,
                        "reply_source": reply_source,
                        "llm_parse_ok": bool(parsed),
                        "llm_parsed": elicitation_parsed_for_trace(
                            parsed, last_user_text=last_user_text
                        ),
                        "missing_after_merge": missing_fb,
                        "city_after_merge": new_req.get("city"),
                        "city_slug_after_merge": new_req.get("city_slug"),
                        "location_after_merge": new_req.get("location"),
                        "location_meta": loc_meta,
                        "location_tools": location_tool_trace,
                        "requirements_complete": req_complete,
                    },
                ),
            }

        async def validate_requirements_node(state: RecState) -> RecState:
            """
            Детерминированная проверка полноты требований без LLM.
            Вычисляет missing_globally, unresolved, not_yet для следующего хода collect.
            """
            req = attach_city_slug_from_reference(
                dict(state.get("recommendation_requirements") or {})
            )
            prior_elic = dict(state.get("elicitation_prior_turn") or {})
            prior_slots = list(prior_elic.get("asked_slots") or [])

            ref_slug = str(req.get("city_slug") or "").strip().lower()
            v_districts: List[Dict[str, str]] = []
            v_metros: List[str] = []
            if location_reference_enabled(ref_slug) and catalog_repo is not None:
                v_districts = await asyncio.to_thread(catalog_repo.list_city_districts, ref_slug)
                v_metros = await asyncio.to_thread(
                    catalog_repo.list_distinct_metro_names, ref_slug
                )
            if location_reference_enabled(ref_slug) and v_districts:
                missing = validate_recommendation_requirements_fields_with_location(
                    req,
                    districts=v_districts,
                    metro_names=v_metros,
                    base_validate=validate_recommendation_requirements_fields,
                )
            else:
                missing = validate_recommendation_requirements_fields(req)
            unresolved = [s for s in missing if s in set(prior_slots)]
            not_yet = [s for s in missing if s not in set(prior_slots)]

            return {
                **state,
                "current_node": "validate_requirements",
                "missing_globally": missing,
                "unresolved_from_last_question": unresolved,
                "not_yet_prompted": not_yet,
                "requirements_complete": len(missing) == 0,
                "pipeline_trace": _trace_append(
                    state,
                    "validate_requirements",
                    {
                        "missing": missing,
                        "unresolved": unresolved,
                        "not_yet": not_yet,
                        "intent": req.get("intent"),
                    },
                ),
            }

        async def elicitation_await_user_node(state: RecState) -> RecState:
            """
            Требования ещё неполные: ответ уже в state['reply'] от collect.
            Завершаем HTTP-ход (END); следующее сообщение пользователя снова войдёт в collect.
            """
            reply = str(state.get("reply") or "").strip()
            if not reply:
                reply = _COLLECT_LLM_RECOVERY_REPLY
            return {
                **state,
                "current_node": "elicitation_await_user",
                "reply": reply,
                "pipeline_trace": _trace_append(
                    state,
                    "elicitation_await_user",
                    {"missing": state.get("missing_globally") or []},
                ),
            }

        # ── Build graph ─────────────────────────────────────────────────────

        graph = StateGraph(RecState)

        # Новые узлы фазы сбора требований
        graph.add_node("collect_requirements", collect_requirements_node)
        graph.add_node("validate_requirements", validate_requirements_node)
        graph.add_node("elicitation_await_user", elicitation_await_user_node)

        # Сохранённые узлы (бронирование, пайплайн подбора, confirm)
        graph.add_node("confirm_search_plan", confirm_search_plan_node)
        graph.add_node("ask_search_plan_revision", ask_search_plan_revision_node)
        graph.add_node("resolve_specific_restaurant", resolve_specific_restaurant_node)
        graph.add_node("load_afisha_candidate_urls", load_afisha_candidate_urls_node)
        graph.add_node("fetch_afisha_cards", fetch_afisha_cards_node)
        graph.add_node("cuisine_prefilter", cuisine_prefilter_node)
        graph.add_node("geo_gate", geo_gate_node)
        graph.add_node("external_rating", external_rating_node)
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

        # ── Entry point и ранняя маршрутизация ──────────────────────────────
        graph.set_entry_point("collect_requirements")

        def route_after_collect(s: RecState) -> str:
            return "validate_requirements"

        def route_after_validate(s: RecState) -> str:
            # Ранние выходы для бронирования (выше любой логики требований)
            if form_booking_payload is not None:
                return "extract_booking_requirements"
            if s.get("booking_pending"):
                return "extract_booking_requirements"
            # Пользователь запросил правку плана
            if s.get("search_plan_revision_requested"):
                return "ask_search_plan_revision"
            # Неполные требования: один LLM-ход на HTTP-запрос, затем END (ответ в reply).
            if s.get("missing_globally"):
                return "elicitation_await_user"
            # Требования полные: план подтверждён — идём в поиск
            req = s.get("recommendation_requirements") or {}
            if bool(s.get("search_plan_confirmed")):
                intent = req.get("intent") or "search"
                if intent == "named_restaurant":
                    return "resolve_specific_restaurant"
                return "load_afisha_candidate_urls"
            # Требования полные, план ещё не подтверждён — показываем confirm
            return "confirm_search_plan"

        graph.add_conditional_edges(
            "collect_requirements",
            route_after_collect,
            path_map={"validate_requirements": "validate_requirements"},
        )
        graph.add_conditional_edges(
            "validate_requirements",
            route_after_validate,
            path_map={
                "elicitation_await_user": "elicitation_await_user",
                "confirm_search_plan": "confirm_search_plan",
                "ask_search_plan_revision": "ask_search_plan_revision",
                "extract_booking_requirements": "extract_booking_requirements",
                "resolve_specific_restaurant": "resolve_specific_restaurant",
                "load_afisha_candidate_urls": "load_afisha_candidate_urls",
            },
        )

        graph.add_edge("elicitation_await_user", END)
        graph.add_edge("confirm_search_plan", END)
        graph.add_edge("ask_search_plan_revision", END)
        graph.add_edge("resolve_specific_restaurant", END)
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

        # После confirm — выбираем ветку поиска или конкретного ресторана
        # confirm_search_plan оканчивается END выше — подтверждение происходит
        # через client_action="confirm_search_plan" в следующем HTTP-запросе.
        # В том запросе collect сразу читает search_plan_confirmed из контекста
        # и направляется в нужную ветку.  Маршрут из confirm нам не нужен.

        # ── Пайплайн подбора (ветка search после confirm) ──────────────────

        graph.add_edge("load_afisha_candidate_urls", "fetch_afisha_cards")
        graph.add_edge("fetch_afisha_cards", "cuisine_prefilter")
        graph.add_edge("cuisine_prefilter", "geo_gate")
        graph.add_edge("geo_gate", "external_rating")
        graph.add_edge("external_rating", "toka_capacity_gate")
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

        def route_after_relax(s: RecState) -> str:
            if int(s.get("relax_attempts") or 0) >= 2 and str(s.get("reply") or "").strip():
                return "format_reply"
            return "load_afisha_candidate_urls"

        graph.add_conditional_edges(
            "relax_fallback",
            route_after_relax,
            path_map={
                "format_reply": "format_reply",
                "load_afisha_candidate_urls": "load_afisha_candidate_urls",
            },
        )

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
            "search_plan_revision_requested": bool(_ctx.get("search_plan_revision_requested")),
            "booking_pending": bool(graph_state.context.get("booking_pending")) if graph_state.context else False,
            "booking_intent_mode": (
                str(_ctx.get("booking_intent_mode"))
                if isinstance(_ctx.get("booking_intent_mode"), str)
                else None
            ),
            "booking_selected_candidate": booking_selected_initial,
            "booking_requirements": (graph_state.context.get("booking_requirements") if graph_state.context else {}) or {},
            "booking_complete": bool(graph_state.context.get("booking_complete")) if graph_state.context else False,
            "booking_missing_fields": (
                graph_state.context.get("booking_missing_fields") if graph_state.context else []
            )
            or [],
            "reservation_result": (graph_state.context.get("reservation_result") if graph_state.context else {}) or {},
            "booking_errors": (graph_state.context.get("booking_errors") if graph_state.context else []) or [],
            "preorder_phase": _ctx.get("preorder_phase"),
            "preorder_menu_available": bool(_ctx.get("preorder_menu_available")),
            "preorder_organization_id": _ctx.get("preorder_organization_id"),
            "preorder_store_id": _ctx.get("preorder_store_id"),
            "preorder_table_id": _ctx.get("preorder_table_id"),
            "preorder_guest_count": _ctx.get("preorder_guest_count"),
            "preorder_cart_lines": _ctx.get("preorder_cart_lines")
            if isinstance(_ctx.get("preorder_cart_lines"), list)
            else [],
            "preorder_order_result": _ctx.get("preorder_order_result"),
            "preorder_receipt_lines": _ctx.get("preorder_receipt_lines")
            if isinstance(_ctx.get("preorder_receipt_lines"), list)
            else [],
            "preorder_receipt_total": _ctx.get("preorder_receipt_total"),
            "receipt_booking_snapshot": (
                _ctx.get("receipt_booking_snapshot")
                if isinstance(_ctx.get("receipt_booking_snapshot"), dict)
                else {}
            ),
            "save_receipt_offered": bool(_ctx.get("save_receipt_offered")),
            "save_receipt_done": bool(_ctx.get("save_receipt_done")),
            "specific_restaurant_requirements": (
                _ctx.get("specific_restaurant_requirements")
                if isinstance(_ctx.get("specific_restaurant_requirements"), dict)
                else {}
            ),
            "specific_restaurant_missing_fields": [
                str(x) for x in (_ctx.get("specific_restaurant_missing_fields") or []) if isinstance(x, str)
            ],
            "specific_restaurant_resolved": bool(_ctx.get("specific_restaurant_resolved")),
            "last_elicitation": (
                _ctx.get("last_elicitation")
                if isinstance(_ctx.get("last_elicitation"), dict)
                else {}
            ),
            "elicitation_turn": int(_ctx.get("elicitation_turn") or 0),
            "elicitation_prior_turn": (
                _ctx.get("elicitation_prior_turn")
                if isinstance(_ctx.get("elicitation_prior_turn"), dict)
                else {}
            ),
            "missing_globally": [
                str(x) for x in (_ctx.get("missing_globally") or []) if isinstance(x, str)
            ],
            "unresolved_from_last_question": [
                str(x)
                for x in (_ctx.get("unresolved_from_last_question") or [])
                if isinstance(x, str)
            ],
            "not_yet_prompted": [
                str(x) for x in (_ctx.get("not_yet_prompted") or []) if isinstance(x, str)
            ],
            "client_time_zone": (
                str(_ctx.get("client_time_zone")).strip()[:128]
                if isinstance(_ctx.get("client_time_zone"), str) and str(_ctx.get("client_time_zone")).strip()
                else None
            ),
            "yandex_queries": [],
            "yandex_urls": [],
            "catalog_entries": [],
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
        # Default LangGraph limit (25) is too low: search path includes up to two relax
        # cycles (load→…→formal_rank→relax→load…), which exceeds 25 node steps.
        try:
            result_state = await asyncio.wait_for(
                app.ainvoke(initial_state, config={"recursion_limit": 100}),
                timeout=_dialog_graph_invoke_timeout_s(),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "dialog graph invoke timed out after %.0fs session_id=%s",
                _dialog_graph_invoke_timeout_s(),
                session_id,
            )
            result_state = {
                **initial_state,
                "reply": _COLLECT_LLM_RECOVERY_REPLY,
                "current_node": "elicitation_await_user",
                "pipeline_trace": _trace_append(
                    initial_state,
                    "dialog_timeout",
                    {"timeout_s": _dialog_graph_invoke_timeout_s()},
                ),
            }

        reply = str(result_state.get("reply") or "")
        trace_events = list(result_state.get("pipeline_trace") or [])
        await self.state_repository.append_pipeline_events(
            session_id=session_id,
            batch_id=trace_batch_id,
            events=trace_events,
        )

        final_context = result_state.copy()
        _ctx_exclude = {
            "session_id",
            "current_node",
            "reply",
            "pipeline_trace",
            # Ephemeral bulk from catalog / SERP — do not persist (can be thousands of rows).
            "catalog_entries",
            "yandex_urls",
            "yandex_queries",
            "candidates",
            "scored_candidates",
        }

        await self.state_repository.append_history(
            session_id=session_id,
            messages=messages,
            reply=reply,
        )
        client_context = sanitize_context_for_client(
            {k: v for k, v in final_context.items() if k not in _ctx_exclude}
        )
        await self.state_repository.update_current_node_and_context(
            session_id=session_id,
            current_node=str(result_state.get("current_node") or "format_reply"),
            context=client_context,
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

        catalog_repo = AfishaCatalogRepository(session_maker)
        graph_runner_singleton = GraphRunner(
            session_store=session_store,
            state_repository=state_repo,
            llm_registry=llm_registry,
            afisha_catalog_repository=catalog_repo,
        )
    return graph_runner_singleton

