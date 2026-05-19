"""
Fetch canonical metro station lists from public sources (Wikipedia).

Data license: Wikipedia/Wikidata content under CC BY-SA (attribute in ops docs if needed).
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import List, Optional

_WIKI_API = "https://ru.wikipedia.org/w/api.php"
_WIKI_USER_AGENT = "restaurant-assistant/1.0 (metro catalog seed; local dev)"
_SPB_WIKI_CATEGORY = "Категория:Станции Петербургского метрополитена"
_MSK_WIKI_METRO_CATEGORY = "Категория:Станции Московского метрополитена"
_MSK_WIKI_DISTRICT_CATEGORY = "Категория:Районы Москвы"


def _wiki_get(params: dict) -> dict:
    url = f"{_WIKI_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _WIKI_USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wiki_category_titles(cmtitle: str) -> List[str]:
    titles: List[str] = []
    cmcontinue: Optional[str] = None
    while True:
        params: dict = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": cmtitle,
            "cmlimit": "500",
            "format": "json",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        data = _wiki_get(params)
        for row in data.get("query", {}).get("categorymembers", []):
            t = row.get("title")
            if isinstance(t, str) and t.strip():
                titles.append(t.strip())
        cmcontinue = data.get("continue", {}).get("cmcontinue")
        if not cmcontinue:
            break
    return titles


def wikipedia_title_to_station_label(title: str) -> Optional[str]:
    t = (title or "").strip()
    if not t or t.startswith("Список") or t.startswith("Категория:"):
        return None
    t = re.sub(r"\s*\(станция метро[^)]*\)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(метро\)\s*$", "", t, flags=re.IGNORECASE)
    return t.strip() or None


def fetch_spb_metro_stations_from_wikipedia() -> List[str]:
    """
    Operational stations of Saint Petersburg Metro from ru.wikipedia category.
    Returns sorted unique display labels (Russian).
    """
    return _fetch_metro_stations_from_wiki_category(_SPB_WIKI_CATEGORY)


def wikipedia_title_to_msk_district_label(title: str) -> Optional[str]:
    t = (title or "").strip()
    if not t or t in {"Районы Москвы"} or t.startswith("Категория:") or t.startswith("Список"):
        return None
    t = re.sub(r"\s*\(Москва\)\s*$", "", t)
    t = re.sub(r"\s*\(район Москвы\)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(муниципальный округ\)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(поселение[^)]*\)\s*$", "", t, flags=re.IGNORECASE)
    return t.strip() or None


def fetch_msk_districts_from_wikipedia() -> List[str]:
    """
    Administrative districts of Moscow (132) from ru.wikipedia category.
    Returns sorted unique display labels (Russian).
    """
    titles = _wiki_category_titles(_MSK_WIKI_DISTRICT_CATEGORY)
    labels: List[str] = []
    seen: set[str] = set()
    for title in titles:
        label = wikipedia_title_to_msk_district_label(title)
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return sorted(labels, key=lambda x: x.casefold())


def _fetch_metro_stations_from_wiki_category(category: str) -> List[str]:
    titles = _wiki_category_titles(category)
    labels: List[str] = []
    seen: set[str] = set()
    for title in titles:
        label = wikipedia_title_to_station_label(title)
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return sorted(labels, key=lambda x: x.casefold())


def fetch_msk_metro_stations_from_wikipedia() -> List[str]:
    """
    Stations of Moscow Metro from ru.wikipedia category (incl. lines with duplicate names).
    Returns sorted unique display labels (Russian); same norm key collapses homonyms.
    """
    return _fetch_metro_stations_from_wiki_category(_MSK_WIKI_METRO_CATEGORY)
