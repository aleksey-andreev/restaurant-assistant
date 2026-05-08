from __future__ import annotations

import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# Yandex SERP XML often includes maps links when query is about a venue.
# Rating like 4,7 or 4.7 near "рейтинг" or in snippet (conservative).
_RE_RATING_COMMA = re.compile(
    r"(?:рейтинг|оценка)\s*[:\-]?\s*(\d)\s*[,]\s*(\d)(?:\s*/\s*5)?",
    re.IGNORECASE,
)
_RE_RATING_DOT = re.compile(
    r"(?:рейтинг|оценка)\s*[:\-]?\s*(\d)\.(\d)(?:\s*/\s*5)?",
    re.IGNORECASE,
)
_RE_SIMPLE = re.compile(r"\b(\d)\s*[,]\s*(\d)\s*(?:из\s*5|/5)\b", re.IGNORECASE)


def _to_float(a: str, b: str) -> float:
    return float(f"{a}.{b}")


def extract_rating_from_yandex_serp_xml(raw_xml: str, *, restaurant_name: str) -> Tuple[Optional[float], float]:
    """
    Best-effort parse of Yandex web search XML. Returns (rating_0_to_5_or_none, confidence 0..1).
    """
    if not raw_xml or len(raw_xml) < 50:
        return None, 0.0
    text = raw_xml.lower()
    has_maps = "yandex.ru/maps" in text
    name_lc = (restaurant_name or "").strip().lower()
    name_hit = bool(name_lc) and name_lc in text

    m = _RE_RATING_COMMA.search(raw_xml)
    if not m:
        m = _RE_RATING_DOT.search(raw_xml)
    if not m:
        m = _RE_SIMPLE.search(raw_xml)
    if not m:
        return None, 0.0

    val = _to_float(m.group(1), m.group(2))
    if val < 1.0 or val > 5.0:
        return None, 0.0

    conf = 0.35
    if has_maps:
        conf += 0.35
    if name_hit:
        conf += 0.25
    return val, min(1.0, conf)


def rating_score_normalized(rating: Optional[float], confidence: float) -> Optional[float]:
    if rating is None or confidence < 0.45:
        return None
    return max(0.0, min(1.0, float(rating) / 5.0))


def stored_yandex_rating_from_catalog(cand: Any) -> Tuple[Optional[float], float]:
    """
    Yandex SERP rating persisted on catalog row during ``sync_afisha_catalog --enrich``.
    """
    if not isinstance(cand, dict):
        return None, 0.0
    r = cand.get("yandex_rating")
    if r is None:
        return None, 0.0
    try:
        val = float(r)
    except (TypeError, ValueError):
        return None, 0.0
    if val < 1.0 or val > 5.0:
        return None, 0.0
    c = cand.get("yandex_rating_confidence")
    try:
        cf = float(c) if c is not None else 0.92
    except (TypeError, ValueError):
        cf = 0.92
    return val, min(1.0, max(0.45, cf))


def catalog_aggregate_rating_score(card_extras: Any) -> Tuple[Optional[float], float]:
    """
    Rating from Afisha card JSON-LD persisted in ``card_extras.from_ld.aggregate_rating``
    (filled by catalog enrich). Returns (0..5 rating or None, confidence 0..1).
    """
    if not isinstance(card_extras, dict):
        return None, 0.0
    from_ld = card_extras.get("from_ld")
    if not isinstance(from_ld, dict):
        return None, 0.0
    ar = from_ld.get("aggregate_rating")
    if not isinstance(ar, dict):
        return None, 0.0
    rv = ar.get("rating_value")
    try:
        val = float(rv)
    except (TypeError, ValueError):
        return None, 0.0
    if val < 1.0 or val > 5.0:
        return None, 0.0
    rc = ar.get("review_count")
    try:
        n = int(rc) if rc is not None else 0
    except (TypeError, ValueError):
        n = 0
    # Structured Afisha LD is a strong signal; modest boost from review count.
    conf = 0.88
    if n >= 50:
        conf = 0.95
    elif n >= 10:
        conf = 0.92
    elif n >= 1:
        conf = 0.90
    return val, min(1.0, conf)


def external_rating_use_yandex() -> bool:
    return os.environ.get("EXTERNAL_RATING_USE_YANDEX", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def build_yandex_rating_query(
    restaurant_name: str,
    city: str,
    *,
    address: Optional[str] = None,
    max_query_len: int = 200,
) -> str:
    """
    Yandex query for venue rating. Includes Afisha address when present so chain outlets
    are distinguished from each other.
    """
    name = (restaurant_name or "").strip()
    city_s = (city or "").strip()
    if not name:
        return ""
    addr = (address or "").strip() if isinstance(address, str) else ""
    core = f"рейтинг ресторана {name}"
    if addr:
        q = f"{core} {addr} {city_s}".strip()
    else:
        q = f"{core} {city_s}".strip()
    q = " ".join(q.split())
    if len(q) <= max_query_len:
        return q
    room = max(1, max_query_len - 1)
    if len(core) >= room:
        return core[:room] + "…"
    return q[:room] + "…"


async def enrich_candidate_external_rating(
    yandex_client: Any,
    *,
    restaurant_name: str,
    city: str,
    address: Optional[str] = None,
    max_query_len: int = 200,
) -> Tuple[Optional[float], float]:
    """
    One Yandex web search + parse. yandex_client must have async search_raw_xml.
    """
    name = (restaurant_name or "").strip()
    if not name:
        return None, 0.0
    q = build_yandex_rating_query(
        restaurant_name,
        city,
        address=address,
        max_query_len=max_query_len,
    )
    if not q:
        return None, 0.0
    try:
        raw = await yandex_client.search_raw_xml(q, page=0)
    except Exception:
        return None, 0.0
    return extract_rating_from_yandex_serp_xml(raw, restaurant_name=name)


def _clamp_to_five(value: float, scale_max: float) -> Optional[float]:
    if scale_max <= 0:
        return None
    out = float(value) * 5.0 / float(scale_max)
    if out < 1.0 or out > 5.0:
        return None
    return out


def _source_boost(source_owner: str) -> float:
    s = (source_owner or "").strip().lower()
    if "yandex" in s:
        return 0.96
    if "2гис" in s or "2gis" in s:
        return 0.93
    if "tripadvisor" in s:
        return 0.90
    if "restaurant" in s and "guru" in s:
        return 0.88
    return 0.82


async def extract_rating_signals_with_llm(
    llm_chat: Callable[..., Awaitable[str]],
    *,
    raw_text: str,
    restaurant_name: str,
    city: str,
    node_params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not raw_text.strip():
        return []
    sys_prompt = (
        "Извлеки из SERP-текста только факты о рейтингах конкретного ресторана.\n"
        "Верни JSON-объект: {\"ratings\": [ ... ]}.\n"
        "Каждый элемент ratings: "
        "{\"source_owner\": string, \"scale_min\": number, \"scale_max\": number, \"value\": number, \"evidence_text\": string}.\n"
        "source_owner примеры: yandex, 2gis, tripadvisor, restaurant_guru, zoon, google.\n"
        "Если рейтингов нет — верни пустой массив. Никаких комментариев."
    )
    user_prompt = (
        f"Ресторан: {restaurant_name}\n"
        f"Город: {city}\n"
        "SERP raw text:\n"
        f"{raw_text[:12000]}"
    )
    params = {**node_params, "response_format": {"type": "json_object"}}
    try:
        raw = await llm_chat(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **params,
        )
    except Exception:
        return []
    try:
        s = raw.find("{")
        e = raw.rfind("}")
        body = raw[s : e + 1] if s >= 0 and e > s else raw
        import json

        payload = json.loads(body)
        arr = payload.get("ratings") if isinstance(payload, dict) else None
        if not isinstance(arr, list):
            return []
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for x in arr:
        if not isinstance(x, dict):
            continue
        owner = str(x.get("source_owner") or "").strip()
        ev = str(x.get("evidence_text") or "").strip()
        try:
            smin = float(x.get("scale_min"))
            smax = float(x.get("scale_max"))
            val = float(x.get("value"))
        except (TypeError, ValueError):
            continue
        if not owner or smax <= smin:
            continue
        if val < smin or val > smax:
            continue
        norm5 = _clamp_to_five(val, smax)
        if norm5 is None:
            continue
        out.append(
            {
                "source_owner": owner,
                "scale_min": smin,
                "scale_max": smax,
                "value": val,
                "value_norm_5": norm5,
                "evidence_text": ev[:300],
            }
        )
    return out


async def enrich_candidate_external_rating_structured(
    yandex_client: Any,
    *,
    restaurant_name: str,
    city: str,
    address: Optional[str] = None,
    max_query_len: int = 200,
    llm_chat: Optional[Callable[..., Awaitable[str]]] = None,
    node_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Returns dict with rating/confidence and optional extracted source records.
    """
    out = {"rating": None, "confidence": 0.0, "sources": []}
    name = (restaurant_name or "").strip()
    if not name:
        return out
    q = build_yandex_rating_query(
        restaurant_name,
        city,
        address=address,
        max_query_len=max_query_len,
    )
    if not q:
        return out
    try:
        raw = await yandex_client.search_raw_xml(q, page=0)
    except Exception:
        return out
    if llm_chat is not None and isinstance(node_params, dict):
        src = await extract_rating_signals_with_llm(
            llm_chat,
            raw_text=raw,
            restaurant_name=name,
            city=city,
            node_params=node_params,
        )
        if src:
            best = max(src, key=lambda x: (_source_boost(str(x.get("source_owner"))), float(x.get("value_norm_5") or 0.0)))
            out["rating"] = float(best["value_norm_5"])
            out["confidence"] = _source_boost(str(best.get("source_owner")))
            out["sources"] = src
            return out
    r, c = extract_rating_from_yandex_serp_xml(raw, restaurant_name=name)
    out["rating"] = r
    out["confidence"] = c
    return out
