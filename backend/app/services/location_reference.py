"""
Canonical district / metro lookup for elicitation and geo_gate (city-scoped).

Supported cities (``LOCATION_REFERENCE_CITY_SLUGS``): districts from ``city_districts``,
metro from ``city_metro_stations`` (fallback: catalog ``geo_osm_metros``).
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..storage.afisha_catalog_repository import norm_city_district_key
from .afisha_city_slug import display_city_label_for_slug

# Cities with reference-backed location normalization in collect / validate.
LOCATION_REFERENCE_CITY_SLUGS = frozenset({"spb", "msk"})

_DISTRICT_QUERY_EXAMPLES: Dict[str, str] = {
    "spb": "«центр», «василеостровский»",
    "msk": "«тверской», «хамовники», «арбат»",
}
_METRO_QUERY_EXAMPLES: Dict[str, str] = {
    "spb": "«невский проспект», «площадь восстания»",
    "msk": "«тверская», «киевская», «сокол»",
}

_DISTRICT_AUTO_PICK_MIN_SCORE = 0.78
_METRO_AUTO_PICK_MIN_SCORE = 0.82

def supported_location_reference_cities_hint() -> str:
    """Human-readable list for prompts (e.g. «Санкт-Петербург (spb), Москва (msk)»)."""
    parts = [
        f"{display_city_label_for_slug(slug)} ({slug})"
        for slug in sorted(LOCATION_REFERENCE_CITY_SLUGS)
    ]
    return ", ".join(parts)


def build_collect_requirements_location_hint(city_slug: str) -> str:
    """Extra system-prompt block when city has seeded districts/metro."""
    slug = str(city_slug or "").strip().lower()
    if not location_reference_enabled(slug):
        return ""
    city_label = display_city_label_for_slug(slug)
    return (
        f"\n\n{city_label}: если пользователь указал район или метро, "
        "вызови search_districts или search_metro с query из его слов. "
        "В location запиши каноническое значение из candidates "
        "(district_label для area, metro_name для metro). "
        "Если кандидатов несколько — уточни у пользователя своими словами."
    )


def elicitation_location_tools_for_city(city_slug: str) -> List[Dict[str, Any]]:
    slug = str(city_slug or "").strip().lower()
    district_ex = _DISTRICT_QUERY_EXAMPLES.get(slug, "фрагмент названия района")
    metro_ex = _METRO_QUERY_EXAMPLES.get(slug, "название станции")
    cities_hint = supported_location_reference_cities_hint()
    return [
        {
            "type": "function",
            "function": {
                "name": "search_districts",
                "description": (
                    "Поиск официального административного района города по фрагменту текста пользователя. "
                    f"Доступно для городов со справочником ({cities_hint}), когда city_slug уже в черновике. "
                    "В location.value записывай district_label из ответа."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": f"Слова пользователя о районе, например {district_ex}",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_metro",
                "description": (
                    "Поиск станции метро по фрагменту текста пользователя. "
                    f"Доступно для городов со справочником ({cities_hint}). "
                    "В location.value записывай metro_name из ответа."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": f"Слова пользователя о метро, например {metro_ex}",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]


def norm_location_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    t = re.sub(r"\s+", " ", value.strip().lower())
    t = t.replace("ё", "е")
    return t


def norm_metro_query(raw: str) -> str:
    t = norm_location_token(raw)
    for prefix in ("станция ", "метро ", "м ", "м."):
        if t.startswith(prefix):
            t = t[len(prefix) :].strip()
    return t


def _norm_district_query(raw: str) -> str:
    t = norm_location_token(raw)
    return t.replace(" р-н", " район")


def _score_substring(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if query in candidate:
        return 0.88
    if candidate in query:
        return 0.85
    q_tokens = [x for x in query.split() if x]
    if q_tokens and all(any(qt in dt or dt.startswith(qt) for dt in candidate.split()) for qt in q_tokens):
        return 0.8
    return float(SequenceMatcher(None, query, candidate).ratio())


def search_districts(
    districts: List[Dict[str, str]],
    query: str,
    *,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Rank districts; each row has district_label, district_norm."""
    q = _norm_district_query(query)
    if not q or not districts:
        return []
    q_alt = q if q.endswith("район") else f"{q} район"
    scored: List[Tuple[float, Dict[str, str]]] = []
    for row in districts:
        label = str(row.get("district_label") or "").strip()
        norm = str(row.get("district_norm") or norm_city_district_key(label))
        if not norm:
            continue
        score = max(
            _score_substring(q, norm),
            _score_substring(q_alt, norm),
            _score_substring(q, norm_city_district_key(label)),
        )
        if score >= 0.45:
            scored.append((score, {"district_label": label, "district_norm": norm}))
    scored.sort(key=lambda x: (-x[0], x[1]["district_label"]))
    lim = max(1, min(int(limit), 20))
    return [
        {**row, "score": round(score, 3)}
        for score, row in scored[:lim]
    ]


def search_metro(
    metro_names: List[str],
    query: str,
    *,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    q = norm_metro_query(query)
    if not q or not metro_names:
        return []
    scored: List[Tuple[float, str]] = []
    seen: set[str] = set()
    for name in metro_names:
        raw = str(name or "").strip()
        if not raw:
            continue
        norm = norm_metro_query(raw)
        if norm in seen:
            continue
        seen.add(norm)
        score = _score_substring(q, norm)
        if score >= 0.45:
            scored.append((score, raw))
    scored.sort(key=lambda x: (-x[0], x[1]))
    lim = max(1, min(int(limit), 20))
    return [
        {"metro_name": name, "metro_norm": norm_metro_query(name), "score": round(score, 3)}
        for score, name in scored[:lim]
    ]


def location_reference_enabled(city_slug: Optional[str]) -> bool:
    s = str(city_slug or "").strip().lower()
    return s in LOCATION_REFERENCE_CITY_SLUGS


def location_is_canonical(
    loc: Dict[str, Any],
    *,
    city_slug: str,
    districts: List[Dict[str, str]],
    metro_names: List[str],
) -> bool:
    if not location_reference_enabled(city_slug):
        return True
    loc_t = loc.get("type")
    loc_v = loc.get("value")
    if loc_t not in {"metro", "area"} or not isinstance(loc_v, str) or not loc_v.strip():
        return False
    if loc_t == "area":
        v_norm = _norm_district_query(loc_v)
        for row in districts:
            label = str(row.get("district_label") or "").strip()
            norm = str(row.get("district_norm") or norm_city_district_key(label))
            if v_norm == norm or v_norm == norm_city_district_key(label):
                return True
        return False
    v_norm = norm_metro_query(loc_v)
    metro_norms = {norm_metro_query(n) for n in metro_names if n}
    return v_norm in metro_norms


def apply_canonical_location_to_req(
    req: Dict[str, Any],
    *,
    districts: List[Dict[str, str]],
    metro_names: List[str],
    district_min_score: float = _DISTRICT_AUTO_PICK_MIN_SCORE,
    metro_min_score: float = _METRO_AUTO_PICK_MIN_SCORE,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
  If a single strong match exists, rewrite location.value to canonical label/name.
  Returns (updated_req, meta for pipeline trace).
    """
    meta: Dict[str, Any] = {}
    slug = str(req.get("city_slug") or "").strip().lower()
    if not location_reference_enabled(slug):
        return req, meta
    loc = req.get("location")
    if not isinstance(loc, dict):
        return req, meta
    loc_t = loc.get("type")
    raw_v = loc.get("value")
    if loc_t not in {"metro", "area"} or not isinstance(raw_v, str) or not raw_v.strip():
        return req, meta

    out = dict(req)
    if loc_t == "area":
        hits = search_districts(districts, raw_v, limit=5)
        meta["district_search"] = {"query": raw_v, "candidates": hits}
        if hits and hits[0]["score"] >= district_min_score:
            second_score = hits[1]["score"] if len(hits) > 1 else 0.0
            if len(hits) == 1 or hits[0]["score"] - second_score >= 0.2:
                out["location"] = {"type": "area", "value": hits[0]["district_label"]}
                meta["location_auto"] = hits[0]
    elif loc_t == "metro":
        hits = search_metro(metro_names, raw_v, limit=5)
        meta["metro_search"] = {"query": raw_v, "candidates": hits}
        if hits and hits[0]["score"] >= metro_min_score:
            second_score = hits[1]["score"] if len(hits) > 1 else 0.0
            if len(hits) == 1 or hits[0]["score"] - second_score >= 0.2:
                out["location"] = {"type": "metro", "value": hits[0]["metro_name"]}
                meta["location_auto"] = hits[0]
    return out, meta


def execute_elicitation_location_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    city_slug: str,
    districts: List[Dict[str, str]],
    metro_names: List[str],
) -> Dict[str, Any]:
    slug = str(city_slug or "").strip().lower()
    if not location_reference_enabled(slug):
        return {
            "ok": False,
            "error": "location_reference_unavailable",
            "message": (
                "Справочник районов/метро доступен только для: "
                f"{supported_location_reference_cities_hint()}."
            ),
        }
    query = str((arguments or {}).get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "empty_query"}

    if tool_name == "search_districts":
        candidates = search_districts(districts, query, limit=10)
        return {
            "ok": True,
            "city_slug": slug,
            "query": query,
            "candidates": candidates,
        }
    if tool_name == "search_metro":
        candidates = search_metro(metro_names, query, limit=15)
        return {
            "ok": True,
            "city_slug": slug,
            "query": query,
            "candidates": candidates,
        }
    return {"ok": False, "error": f"unknown_tool:{tool_name}"}


def format_location_tool_result(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def validate_recommendation_requirements_fields_with_location(
    req: Dict[str, Any],
    *,
    districts: Optional[List[Dict[str, str]]] = None,
    metro_names: Optional[List[str]] = None,
    base_validate: Callable[[Dict[str, Any]], List[str]],
) -> List[str]:
    """search + spb/msk: metro/area must match справочник, иначе location_or_cuisine остаётся missing."""
    missing = list(base_validate(req))
    intent = req.get("intent") or "search"
    if intent == "named_restaurant":
        return missing
    slug = str(req.get("city_slug") or "").strip().lower()
    if not location_reference_enabled(slug):
        return missing
    loc = req.get("location")
    if not isinstance(loc, dict) or loc.get("type") not in {"metro", "area"}:
        return missing
    d = districts if districts is not None else []
    m = metro_names if metro_names is not None else []
    if location_is_canonical(loc, city_slug=slug, districts=d, metro_names=m):
        return [x for x in missing if x != "location_or_cuisine"]
    if "location_or_cuisine" not in missing:
        missing.append("location_or_cuisine")
    return missing


async def run_elicitation_llm_with_location_tools(
    llm_client: Any,
    *,
    system_prompt: str,
    user_prompt: str,
    node_params: Dict[str, Any],
    city_slug: str,
    districts: List[Dict[str, str]],
    metro_names: List[str],
    parse_json: Callable[[Optional[str]], Dict[str, Any]],
    max_tool_rounds: int = 2,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Chat loop with search_districts / search_metro tools, then JSON parse from final content.
    Returns (parsed_criteria_dict, tool_trace).
    """
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    params = dict(node_params)
    params.pop("response_format", None)
    tool_trace: List[Dict[str, Any]] = []
    parsed: Dict[str, Any] = {}

    for _ in range(max_tool_rounds):
        try:
            response = await llm_client.chat_completion(
                messages=messages,
                tools=elicitation_location_tools_for_city(city_slug),
                tool_choice="auto",
                **params,
            )
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower():
                tool_trace.append({"tool": "_timeout", "result": {}})
                break
            raise
        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = execute_elicitation_location_tool(
                    name,
                    args if isinstance(args, dict) else {},
                    city_slug=city_slug,
                    districts=districts,
                    metro_names=metro_names,
                )
                tool_trace.append({"tool": name, "args": args, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": format_location_tool_result(result),
                    }
                )
            continue

        content = (msg.content or "").strip()
        if content:
            parsed = parse_json(content)
        break

    if not parsed:
        params_json = {**node_params, "response_format": {"type": "json_object"}}
        try:
            final = await llm_client.chat(
                messages=messages
                + [
                    {
                        "role": "user",
                        "content": (
                            "Сформируй итоговый ответ строго одним JSON-объектом по схеме из system "
                            "(intent, city, location, user_reply, asked_slots, …). Без пояснений."
                        ),
                    }
                ],
                **params_json,
            )
            parsed = parse_json(final)
            tool_trace.append({"tool": "_final_json", "result": {"ok": bool(parsed)}})
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower():
                tool_trace.append({"tool": "_final_json_timeout", "result": {}})
            else:
                raise

    return parsed, tool_trace
