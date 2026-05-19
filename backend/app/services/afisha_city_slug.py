"""
Resolve Afisha www.afisha.ru city path segment (city_slug) from a canonical city label.

Slugs match Afisha URL prefixes (e.g. msk, spb, voronezh).
Lookup is reference-only: official / standard city names → slug. No slang (Питер, Владик)
and no transliteration guesses — the dialog LLM must normalize user wording first.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional

# Normalized keys: NFKC lower, "ё"->"е", "г." stripped, "-" spaces collapsed.
# Only canonical names (and standard Latin forms). Values: Afisha path segment.
_CITY_REFERENCE: Dict[str, str] = {
    "москва": "msk",
    "moscow": "msk",
    "moskva": "msk",
    "санкт петербург": "spb",
    "санктпетербург": "spb",
    "saint petersburg": "spb",
    "sankt peterburg": "spb",
    "sankt-peterburg": "spb",
    "воронеж": "voronezh",
    "voronezh": "voronezh",
    "владивосток": "vladivostok",
    "vladivostok": "vladivostok",
    "ростов на дону": "rostov-na-donu",
    "rostov on don": "rostov-na-donu",
    "rostov-na-donu": "rostov-na-donu",
    "екатеринбург": "ekaterinburg",
    "yekaterinburg": "ekaterinburg",
    "ekaterinburg": "ekaterinburg",
    "новосибирск": "novosibirsk",
    "novosibirsk": "novosibirsk",
    "казань": "kazan",
    "kazan": "kazan",
    "самара": "samara",
    "samara": "samara",
    "уфа": "ufa",
    "ufa": "ufa",
    "красноярск": "krasnoyarsk",
    "krasnoyarsk": "krasnoyarsk",
    "омск": "omsk",
    "omsk": "omsk",
    "челябинск": "chelyabinsk",
    "chelyabinsk": "chelyabinsk",
    "краснодар": "krasnodar",
    "krasnodar": "krasnodar",
    "тула": "tula",
    "tula": "tula",
    "тверь": "tver",
    "tver": "tver",
    "сочи": "sochi",
    "sochi": "sochi",
    "иркутск": "irkutsk",
    "irkutsk": "irkutsk",
    "нижний новгород": "nnovgorod",
    "nizhniy novgorod": "nnovgorod",
    "nnovgorod": "nnovgorod",
    "калининград": "kaliningrad",
    "kaliningrad": "kaliningrad",
}

# Human-readable city label for LLM / prompts (by Afisha slug).
_SLUG_TO_DISPLAY: Dict[str, str] = {
    "msk": "Москва",
    "spb": "Санкт-Петербург",
    "voronezh": "Воронеж",
    "vladivostok": "Владивосток",
    "rostov-na-donu": "Ростов-на-Дону",
    "ekaterinburg": "Екатеринбург",
    "novosibirsk": "Новосибирск",
    "kazan": "Казань",
    "samara": "Самара",
    "ufa": "Уфа",
    "krasnoyarsk": "Красноярск",
    "omsk": "Омск",
    "chelyabinsk": "Челябинск",
    "krasnodar": "Краснодар",
    "tula": "Тула",
    "tver": "Тверь",
    "sochi": "Сочи",
    "irkutsk": "Иркутск",
    "nnovgorod": "Нижний Новгород",
    "kaliningrad": "Калининград",
}

_KNOWN_SLUGS = frozenset(_SLUG_TO_DISPLAY.keys())


def _normalize_city_key(label: str) -> str:
    t = unicodedata.normalize("NFKC", (label or "").strip()).lower()
    t = t.replace("ё", "е")
    t = re.sub(r"^г\.?\s*", "", t)
    t = t.replace("-", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def list_supported_city_labels_ru() -> List[str]:
    """Canonical Russian city names for LLM prompts (sorted, unique)."""
    seen: set[str] = set()
    out: List[str] = []
    for label in _SLUG_TO_DISPLAY.values():
        if label not in seen:
            seen.add(label)
            out.append(label)
    return sorted(out)


def display_city_label_for_slug(city_slug: str) -> str:
    """Return a short Russian city name for *city_slug* (Afisha path segment)."""
    s = (city_slug or "").strip().lower()
    if not s:
        return "—"
    if s in _SLUG_TO_DISPLAY:
        return _SLUG_TO_DISPLAY[s]
    return s.replace("-", " ").replace("_", " ").strip().title() or "—"


def resolve_afisha_city_slug(city: Optional[str]) -> Optional[str]:
    """
    Return Afisha city path segment from a **canonical** city label, or None.

    Does not map slang (Питер, Владик, СПб) or transliterate unknown names.
    Accepts an exact Afisha slug if it is in the known catalog.
    """
    if city is None:
        return None
    raw = str(city).strip()
    if not raw:
        return None

    key = _normalize_city_key(raw)
    if not key:
        return None
    if key in _CITY_REFERENCE:
        return _CITY_REFERENCE[key]

    # Allow model/system to pass slug directly when already canonical.
    slug_direct = raw.strip().lower()
    if slug_direct in _KNOWN_SLUGS:
        return slug_direct

    return None
