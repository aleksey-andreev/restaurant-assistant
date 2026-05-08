"""
Geocode Afisha addresses via public OSM services (Nominatim + Overpass).

Intended for background catalog enrich: Nominatim usage policy requires ~1 req/s
and a descriptive User-Agent. Overpass should be used sparingly (batch-friendly).
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .yandex_web_search import YandexWebSearchClient

_NOMINATIM_LOCK = asyncio.Lock()
_LAST_NOMINATIM_MONO: float = 0.0
_SPB_OFFICIAL_DISTRICTS = (
    "адмиралтейский",
    "василеостровский",
    "выборгский",
    "калининский",
    "кировский",
    "колпинский",
    "красногвардейский",
    "красносельский",
    "кронштадтский",
    "курортный",
    "московский",
    "невский",
    "петроградский",
    "петродворцовый",
    "приморский",
    "пушкинский",
    "фрунзенский",
    "центральный",
)
_SPB_DISTRICT_PATTERNS: Dict[str, re.Pattern[str]] = {
    "Адмиралтейский район": re.compile(r"\bадмиралтейск\w*\s+район\b", re.IGNORECASE),
    "Василеостровский район": re.compile(r"\bвасилеостровск\w*\s+район\b", re.IGNORECASE),
    "Выборгский район": re.compile(r"\bвыборгск\w*\s+район\b", re.IGNORECASE),
    "Калининский район": re.compile(r"\bкалининск\w*\s+район\b", re.IGNORECASE),
    "Кировский район": re.compile(r"\bкировск\w*\s+район\b", re.IGNORECASE),
    "Колпинский район": re.compile(r"\bколпинск\w*\s+район\b", re.IGNORECASE),
    "Красногвардейский район": re.compile(r"\bкрасногвардейск\w*\s+район\b", re.IGNORECASE),
    "Красносельский район": re.compile(r"\bкрасносельск\w*\s+район\b", re.IGNORECASE),
    "Кронштадтский район": re.compile(r"\bкронштадтск\w*\s+район\b", re.IGNORECASE),
    "Курортный район": re.compile(r"\bкурортн\w*\s+район\b", re.IGNORECASE),
    "Московский район": re.compile(r"\bмосковск\w*\s+район\b", re.IGNORECASE),
    "Невский район": re.compile(r"\bневск\w*\s+район\b", re.IGNORECASE),
    "Петроградский район": re.compile(r"\bпетроградск\w*\s+район\b", re.IGNORECASE),
    "Петродворцовый район": re.compile(r"\bпетродворцов\w*\s+район\b", re.IGNORECASE),
    "Приморский район": re.compile(r"\bприморск\w*\s+район\b", re.IGNORECASE),
    "Пушкинский район": re.compile(r"\bпушкинск\w*\s+район\b", re.IGNORECASE),
    "Фрунзенский район": re.compile(r"\bфрунзенск\w*\s+район\b", re.IGNORECASE),
    "Центральный район": re.compile(r"\bцентральн\w*\s+район\b", re.IGNORECASE),
}


def _user_agent() -> str:
    return os.environ.get(
        "OSM_HTTP_USER_AGENT",
        "RestaurantAssistant/1.0 (https://github.com/; osm-geo)",
    ).strip()


def _nominatim_base() -> str:
    return (os.environ.get("OSM_NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org") or "").rstrip("/")


def _overpass_url() -> str:
    return (
        os.environ.get("OSM_OVERPASS_API_URL", "https://overpass-api.de/api/interpreter") or ""
    ).rstrip("/")


def _metro_radius_m() -> int:
    try:
        return max(500, min(8000, int(os.environ.get("OSM_METRO_SEARCH_RADIUS_M", "2800"))))
    except (TypeError, ValueError):
        return 2800


def _nominatim_delay_s() -> float:
    try:
        return max(0.5, float(os.environ.get("OSM_NOMINATIM_DELAY_S", "1.1")))
    except (TypeError, ValueError):
        return 1.1


def _overpass_delay_s() -> float:
    try:
        return max(0.0, float(os.environ.get("OSM_OVERPASS_DELAY_S", "2.0")))
    except (TypeError, ValueError):
        return 2.0


async def _nominatim_throttle() -> None:
    delay = _nominatim_delay_s()
    async with _NOMINATIM_LOCK:
        global _LAST_NOMINATIM_MONO
        now = time.monotonic()
        wait = delay - (now - _LAST_NOMINATIM_MONO)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_NOMINATIM_MONO = time.monotonic()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _district_from_nominatim_addr(addr: Any) -> Optional[str]:
    if not isinstance(addr, dict):
        return None
    for key in ("city_district", "suburb", "quarter", "neighbourhood", "district"):
        v = addr.get(key)
        if isinstance(v, str):
            t = re.sub(r"\s+", " ", v).strip()
            if t:
                return t[:256]
    return None


def _normalize_spb_official_district(addr: Any) -> Optional[str]:
    """
    Nominatim address often provides municipal okrug/suburb, not city district.
    Return only one of 18 official Saint-Petersburg districts, or None.
    """
    if not isinstance(addr, dict):
        return None
    # Use explicit administrative fields only; avoid street-name false matches.
    parts: List[str] = []
    for key in ("city_district", "district", "county", "state_district"):
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    blob = " ".join(parts).lower()
    if not blob:
        return None
    for key in _SPB_OFFICIAL_DISTRICTS:
        if re.search(rf"\b{re.escape(key)}(?:\s+район)?\b", blob):
            return f"{key.capitalize()} район"
    return None


def _extract_spb_district_from_yandex_raw(raw_text: str) -> Optional[str]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    scores: Dict[str, int] = {}
    for district, pattern in _SPB_DISTRICT_PATTERNS.items():
        cnt = len(pattern.findall(raw_text))
        if cnt > 0:
            scores[district] = cnt
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


async def _resolve_spb_district_via_yandex_search(
    *,
    address: Optional[str],
    city: str,
) -> Optional[str]:
    addr = (address or "").strip() if isinstance(address, str) else ""
    city_s = (city or "").strip()
    if not addr or not city_s:
        return None
    query = f"административный район по адресу {city_s}, {addr}"
    try:
        raw = await YandexWebSearchClient.from_env().search_raw_xml(query)
    except Exception:
        return None
    return _extract_spb_district_from_yandex_raw(raw)


def _normalize_metro_station_name(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    t = re.sub(r"\s+", " ", value).strip()
    if not t:
        return None
    t = t.strip("\"'`«»")
    t = re.sub(r"^\s*(станция\s+метро|метро|станция|м\.)\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\((?:станция\s+)?метро\)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" -")
    return t or None


def _station_display_name(tags: Dict[str, Any]) -> Optional[str]:
    if not isinstance(tags, dict):
        return None
    for k in ("name:ru", "name:en", "name"):
        v = tags.get(k)
        if isinstance(v, str):
            t = re.sub(r"\s+", " ", v).strip()
            t = _normalize_metro_station_name(t)
            if t and len(t) <= 120:
                return t
    return None


def _likely_subway_station(tags: Dict[str, Any]) -> bool:
    if not isinstance(tags, dict):
        return False
    if tags.get("station") == "subway":
        return True
    if str(tags.get("subway") or "").lower() in {"yes", "true"}:
        return True
    pt = str(tags.get("public_transport") or "").lower()
    if "station" in pt and "subway" in str(tags).lower():
        return True
    if tags.get("railway") == "station" and (
        tags.get("subway") == "yes" or "subway" in str(tags.get("route") or "").lower()
    ):
        return True
    if tags.get("railway") == "station" and isinstance(tags.get("network"), str):
        n = tags["network"].lower()
        if "метро" in n or "metro" in n:
            return True
    return False


@dataclass
class OsmGeoResult:
    """Resolved district + metro list from OSM (cloud APIs)."""

    district: Optional[str]
    metros: List[str]
    primary_metro: Optional[str]
    lat: Optional[float]
    lon: Optional[float]

    @property
    def ok(self) -> bool:
        return self.district is not None or bool(self.metros)


async def _nominatim_search(
    client: httpx.AsyncClient,
    *,
    q: str,
) -> Optional[Dict[str, Any]]:
    if not q.strip():
        return None
    await _nominatim_throttle()
    base = _nominatim_base()
    if not base:
        return None
    params = {
        "q": q,
        "format": "json",
        "limit": "1",
        "addressdetails": "1",
        "accept-language": "ru,en",
    }
    headers = {"User-Agent": _user_agent()}
    resp = await client.get(f"{base}/search", params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


async def _overpass_subway_nodes(
    client: httpx.AsyncClient,
    *,
    lat: float,
    lon: float,
    radius_m: int,
) -> List[Tuple[float, float, Dict[str, Any]]]:
    await asyncio.sleep(_overpass_delay_s())
    url = _overpass_url()
    if not url:
        return []
    r = int(radius_m)
    ql = f"""[out:json][timeout:25];
(
  node["station"="subway"](around:{r},{lat},{lon});
  node["railway"="station"](around:{r},{lat},{lon});
);
out body;"""
    headers = {"User-Agent": _user_agent()}
    resp = await client.post(url, data={"data": ql}, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    els = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(els, list):
        return []
    out: List[Tuple[float, float, Dict[str, Any]]] = []
    for el in els:
        if not isinstance(el, dict) or el.get("type") != "node":
            continue
        tags = el.get("tags")
        if not isinstance(tags, dict):
            continue
        if not _likely_subway_station(tags):
            continue
        try:
            la = float(el["lat"])
            lo = float(el["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((la, lo, tags))
    return out


def _merge_metros_sorted(
    lat: float,
    lon: float,
    nodes: List[Tuple[float, float, Dict[str, Any]]],
    *,
    limit: int = 8,
) -> Tuple[List[str], Optional[str]]:
    scored: List[Tuple[float, str]] = []
    seen: set[str] = set()
    for la, lo, tags in nodes:
        label = _station_display_name(tags)
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        d = _haversine_m(lat, lon, la, lo)
        scored.append((d, label))
    scored.sort(key=lambda x: x[0])
    ordered = [lbl for _d, lbl in scored[:limit]]
    primary = ordered[0] if ordered else None
    return ordered, primary


async def resolve_osm_geo(
    *,
    address: Optional[str],
    name: Optional[str],
    city: str,
    client: httpx.AsyncClient,
) -> OsmGeoResult:
    """
    Forward-geocode (Nominatim) then nearest subway-like stations (Overpass).
    """
    city_s = (city or "").strip()
    queries: List[str] = []
    a = (address or "").strip() if isinstance(address, str) else ""
    nm = (name or "").strip() if isinstance(name, str) else ""
    if a:
        queries.append(a)
    if nm and city_s:
        queries.append(f"{nm}, {city_s}")
    elif nm:
        queries.append(nm)

    hit: Optional[Dict[str, Any]] = None
    for q in queries:
        try:
            hit = await _nominatim_search(client, q=q)
        except Exception:
            hit = None
        if hit:
            break

    if not hit:
        return OsmGeoResult(None, [], None, None, None)

    try:
        lat = float(hit["lat"])
        lon = float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return OsmGeoResult(None, [], None, None, None)

    addr = hit.get("address")
    district = _district_from_nominatim_addr(addr)
    if city_s.lower() in {"санкт-петербург", "spb", "saint petersburg", "saint-petersburg"}:
        district = await _resolve_spb_district_via_yandex_search(address=a or None, city=city_s)

    metros: List[str] = []
    primary: Optional[str] = None
    try:
        nodes = await _overpass_subway_nodes(client, lat=lat, lon=lon, radius_m=_metro_radius_m())
        metros, primary = _merge_metros_sorted(lat, lon, nodes)
    except Exception:
        metros, primary = [], None

    return OsmGeoResult(
        district=district,
        metros=metros,
        primary_metro=primary,
        lat=lat,
        lon=lon,
    )


def osm_geo_enabled() -> bool:
    return os.environ.get("OSM_GEO_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
