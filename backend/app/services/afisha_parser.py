from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from .cuisine_normalize import merge_tag_lists


class AfishaParseError(Exception):
    pass


def _first_or_none(lst: List[Any]) -> Any:
    return lst[0] if lst else None


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_price_range(raw: str) -> Optional[Tuple[int, int]]:
    """
    Parse prices like: '1000–3000 ₽' or '1000 - 3000'
    """
    if not raw:
        return None
    s = raw.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    # Keep digits and separators
    m = re.search(r"(\d[\d\s]*)\s*-\s*(\d[\d\s]*)", s)
    if not m:
        # sometimes it's a single price
        m2 = re.search(r"(\d[\d\s]*)\s*₽", s)
        if m2:
            v = int(m2.group(1).replace(" ", ""))
            return (v, v)
        return None
    a = int(m.group(1).replace(" ", ""))
    b = int(m.group(2).replace(" ", ""))
    return (min(a, b), max(a, b))


def _extract_avg_check(full_text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"Средний чек\s*([0-9][0-9\s\u2013\u2014\-–]*\s*₽?)", full_text)
    if not m:
        return None
    raw = _norm_space(m.group(1))
    # raw might lack '₽' at the end if it was collapsed; try again
    m2 = re.search(
        r"Средний чек\s*([0-9][0-9\s\u2013\u2014\-–]*\s*₽)",
        full_text,
    )
    if m2:
        raw = _norm_space(m2.group(1))
    parsed = _parse_price_range(raw)
    if not parsed:
        return None
    min_v, max_v = parsed
    return {"raw": raw, "min": min_v, "max": max_v}


def _extract_open_now(full_text: str) -> Dict[str, Any]:
    # Examples from Afisha:
    # "Открыто c 12:00 до 00:00"
    # "Открыто до 23:00"
    m = re.search(r"Открыто\s+c\s+(\d{1,2}:\d{2})\s+до\s+(\d{1,2}:\d{2})", full_text)
    if m:
        return {"is_open_now": True, "raw": _norm_space(m.group(0)), "from": m.group(1), "to": m.group(2)}
    m2 = re.search(r"Открыто\s+до\s+(\d{1,2}:\d{2})", full_text)
    if m2:
        return {"is_open_now": True, "raw": _norm_space(m2.group(0)), "to": m2.group(1)}
    # If card doesn't say "Открыто", treat as unknown (not closed)
    if "Закрыто" in full_text:
        return {"is_open_now": False, "raw": "Закрыто"}
    return {"is_open_now": None, "raw": None}


def _extract_flags(full_text: str) -> Dict[str, Optional[bool]]:
    flags: Dict[str, Optional[bool]] = {
        "delivery": None,
        "parking": None,
        "catering": None,
        "banquets": None,
        "breakfast": None,
        "business_lunch": None,
    }

    # Afisha cards often use "Доставка Есть/Нет" as separate lines.
    mapping = {
        "Доставка": "delivery",
        "Парковка": "parking",
        "Кейтеринг": "catering",
        "Банкеты": "banquets",
        "Завтраки": "breakfast",
        "Бизнес-ланч": "business_lunch",
    }

    for ru_label, key in mapping.items():
        # capture small window after label
        m = re.search(rf"{re.escape(ru_label)}\s*(Есть|Нет)", full_text)
        if m:
            flags[key] = True if m.group(1) == "Есть" else False
    return flags


def _extract_tags(soup: BeautifulSoup) -> List[str]:
    tags: List[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", ""))
        if "restaurant_list" in href:
            text = _norm_space(a.get_text(" ", strip=True))
            if text and len(text) <= 40:
                tags.append(text)
    # keep order + dedupe
    seen = set()
    out: List[str] = []
    for t in tags:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:15]


def _is_restaurant_ld(node: Dict[str, Any]) -> bool:
    t = node.get("@type")
    if t == "Restaurant":
        return True
    if isinstance(t, list):
        return "Restaurant" in t
    return False


def _walk_find_restaurant(obj: Any) -> Optional[Dict[str, Any]]:
    if isinstance(obj, dict):
        if _is_restaurant_ld(obj):
            return obj
        for v in obj.values():
            r = _walk_find_restaurant(v)
            if r:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = _walk_find_restaurant(x)
            if r:
                return r
    return None


def _extract_ld_restaurant(html: str) -> Optional[Dict[str, Any]]:
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        r = _walk_find_restaurant(data)
        if r:
            return r
    return None


def parse_afisha_restaurant_card(html: str, url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    full_text = _norm_space(soup.get_text("\n", strip=True))
    ld = _extract_ld_restaurant(html)

    name = None
    h1 = soup.find("h1")
    if h1:
        name = _norm_space(h1.get_text(" ", strip=True))
    if not name and ld and ld.get("name"):
        name = _norm_space(str(ld["name"]))

    avg_check = _extract_avg_check(full_text)
    if not avg_check and ld and ld.get("priceRange"):
        pr = str(ld["priceRange"])
        parsed = _parse_price_range(pr.replace("₽", " ₽"))
        if parsed:
            min_v, max_v = parsed
            avg_check = {"raw": _norm_space(pr), "min": min_v, "max": max_v}

    open_now = _extract_open_now(full_text)
    flags = _extract_flags(full_text)
    tags = _extract_tags(soup)
    extras: List[str] = []
    if ld and ld.get("servesCuisine") is not None:
        sc = ld.get("servesCuisine")
        if isinstance(sc, str) and sc.strip():
            extras = [_norm_space(sc)]
        elif isinstance(sc, list):
            extras = [_norm_space(str(x)) for x in sc if x]
    if extras:
        tags = merge_tag_lists(tags, extras)

    # Address is hard to extract robustly, but in Afisha it usually appears near "Подробная информация".
    address = None
    m_city = re.search(r"(Москва, [^О]+Посмотреть на карту)", full_text)
    if m_city:
        address = _norm_space(m_city.group(1).replace("Посмотреть на карту", "")).strip(" ,")
    if not address and ld and isinstance(ld.get("address"), dict):
        a = ld["address"]
        loc = a.get("addressLocality")
        street = a.get("streetAddress")
        parts = [p for p in (loc, street) if isinstance(p, str) and p.strip()]
        if parts:
            address = _norm_space(", ".join(parts))

    metro = None
    m_metro = re.search(r"\b([А-ЯA-Z][а-яa-z\- ]{2,30})\b\s*Посмотреть на карту", full_text)
    if m_metro:
        metro = _norm_space(m_metro.group(1))
    if not metro and ld and isinstance(ld.get("address"), dict):
        sa = ld["address"].get("streetAddress")
        if isinstance(sa, str) and "," in sa:
            metro = _norm_space(sa.split(",")[0])
        elif isinstance(sa, str) and sa.strip():
            metro = _norm_space(sa)

    return {
        "url": url,
        "name": name,
        "address": address,
        "metro": metro,
        "avg_check": avg_check,
        "open_now": open_now,
        "flags": flags,
        "tags": tags,
        # raw text is useful for debug/LLM but keep it out of scoring by default
        "debug": {"has_full_text": bool(full_text), "full_text_len": len(full_text)},
    }


async def fetch_and_parse_afisha_card(
    url: str,
    *,
    timeout_s: float = 20.0,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    return parse_afisha_restaurant_card(html, url=url)

