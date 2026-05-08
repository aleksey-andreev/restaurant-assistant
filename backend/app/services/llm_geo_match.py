from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

GeoResult = str  # "match" | "no_match" | "uncertain"

_RE_STRIP_JSON = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def fingerprint_user_location(loc: Optional[Dict[str, Any]]) -> str:
    if not isinstance(loc, dict):
        return ""
    t = loc.get("type")
    v = loc.get("value")
    if t in {"metro", "area"} and isinstance(v, str):
        return f"{t}:{v.strip().lower()}"
    return ""


def build_restaurant_address_for_geo(cand: Dict[str, Any]) -> str:
    parts: List[str] = []
    addr = cand.get("address")
    if isinstance(addr, str) and addr.strip():
        parts.append(addr.strip())
    name = cand.get("name")
    if not parts and isinstance(name, str) and name.strip():
        parts.append(f"заведение: {name.strip()} (точный адрес не известен)")
    return "; ".join(parts) if parts else ""


def _norm_loc_token(s: str) -> str:
    return " ".join(s.lower().split())


def parse_llm_geo_inference_json(raw: str) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Parse model JSON for inferred metro/area only (no match verdict from the model).
    On failure returns (None, None, False).
    """
    text = _RE_STRIP_JSON.sub("", (raw or "").strip())
    if not text:
        return None, None, False
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None, None, False
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None, None, False
    if not isinstance(data, dict):
        return None, None, False
    im = data.get("inferred_primary_metro")
    ia = data.get("inferred_district_or_area")
    im_s = im.strip() if isinstance(im, str) and im.strip() else None
    ia_s = ia.strip() if isinstance(ia, str) and ia.strip() else None
    return im_s, ia_s, True


def parse_llm_geo_json(raw: str) -> Tuple[GeoResult, Optional[str], Optional[str]]:
    """
    Backward-compatible parse: extracts inferred fields only; verdict is always uncertain
    here — callers should use match_inferred_to_user for match/no_match/uncertain.
    """
    im, ia, ok = parse_llm_geo_inference_json(raw)
    if not ok:
        return "uncertain", None, None
    return "uncertain", im, ia


def match_inferred_to_user(
    inferred_metro: Optional[str],
    inferred_area: Optional[str],
    user_location: Dict[str, Any],
    *,
    extra_metro_names: Optional[List[str]] = None,
) -> GeoResult:
    """
    Compare inferred geography to the user's location slot (metro or area).
    ``extra_metro_names`` — e.g. several nearest stations from OSM (catalog).
    Conservative: missing inference → uncertain; weak signals → uncertain where ambiguous.
    """
    lt = user_location.get("type")
    lv = user_location.get("value")
    if lt not in {"metro", "area"} or not isinstance(lv, str) or not lv.strip():
        return "match"
    want = _norm_loc_token(lv)
    m = _norm_loc_token(inferred_metro) if inferred_metro else ""
    a = _norm_loc_token(inferred_area) if inferred_area else ""

    metro_tokens: List[str] = []
    if m:
        metro_tokens.append(m)
    if isinstance(extra_metro_names, list):
        for x in extra_metro_names:
            if isinstance(x, str) and x.strip():
                t = _norm_loc_token(x)
                if t and t not in metro_tokens:
                    metro_tokens.append(t)

    if not metro_tokens and not a:
        return "uncertain"

    def _overlap(x: str, y: str) -> bool:
        if not x or not y:
            return False
        return x in y or y in x

    if lt == "metro":
        for mm in metro_tokens:
            if mm and _overlap(want, mm):
                return "match"
        if metro_tokens:
            return "no_match"
        return "uncertain"

    # area
    if a and _overlap(want, a):
        return "match"
    if a:
        for mm in metro_tokens:
            if mm and _overlap(want, mm):
                return "uncertain"
        return "no_match"
    for mm in metro_tokens:
        if mm and _overlap(want, mm):
            return "uncertain"
    return "uncertain"


def _cache_key(city: str, address_blob: str, isolation_key: str) -> str:
    """
    isolation_key must differ per candidate (e.g. Afisha card URL) so that two venues
    with the same address string never share one LLM outcome. User location is not part
    of the key: inference is address-only and reusable across sessions.
    """
    h = hashlib.sha256(
        f"{city.lower().strip()}|{address_blob.lower().strip()}|{isolation_key}".encode("utf-8")
    ).hexdigest()
    return h


def location_score_from_result(result: GeoResult) -> float:
    if result == "match":
        return 1.0
    if result == "uncertain":
        return 0.45
    return 0.0


async def llm_geo_infer_one(
    llm_chat: Callable[..., Coroutine[Any, Any, str]],
    *,
    city: str,
    restaurant_address: str,
    restaurant_name: Optional[str],
    node_params: Dict[str, Any],
    cache: Dict[str, Tuple[Optional[str], Optional[str]]],
    isolation_key: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    One stateless LLM request per address: infer nearest metro (if the city has metro)
    and district / municipal area / historic zone for the address. No user preference
    in the prompt — compare inferred strings to the user slot in Python (match_inferred_to_user).
    """
    key = _cache_key(city, restaurant_address, isolation_key)
    if key in cache:
        return cache[key]

    request_id = str(uuid.uuid4())

    sys_prompt = (
        "Ты эксперт по городской топонимике. По городу и точному адресу (и кратким ориентирам "
        "из карточки, если они даны) определи, к какой зоне города относится точка: "
        "ближайшая станция метро (если в этом городе есть метро) и район/округ/территориальная "
        "единица, к которой по общепринятой классификации относится адрес.\n"
        "Не выдумывай координаты; только осмысленные названия метро и района как строки.\n"
        "Если адрес неполный, город неясен или нельзя уверенно назвать метро или район — "
        "ставь null в соответствующих полях.\n"
        "Верни ТОЛЬКО JSON без markdown и без комментариев.\n"
        "Схема JSON:\n"
        '{"inferred_primary_metro": string|null, "inferred_district_or_area": string|null}'
    )
    name_line = f"Название заведения: {restaurant_name}\n" if restaurant_name else ""
    user_msg = (
        f"Город: {city}\n"
        f"{name_line}"
        f"Адрес и ориентиры: {restaurant_address or '(нет)'}\n"
        f"Идентификатор запроса (технический, на ответ не влияет): {request_id}\n"
        "Верни JSON."
    )
    params_req = {**node_params, "response_format": {"type": "json_object"}}
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    try:
        raw = await llm_chat(
            messages=list(messages),
            **params_req,
        )
    except Exception:
        try:
            raw = await llm_chat(
                messages=list(messages),
                **node_params,
            )
        except Exception:
            out: Tuple[Optional[str], Optional[str]] = (None, None)
            cache[key] = out
            return out

    im, ia, ok = parse_llm_geo_inference_json(raw)
    if not ok:
        out = (None, None)
    else:
        out = (im, ia)
    cache[key] = out
    return out
