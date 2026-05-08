"""
Resolve Afisha www.afisha.ru city path segment (city_slug) from a human city label.

Slugs are not ISO codes: they match Afisha URL prefixes (e.g. msk, spb, voronezh).
Verified by canonical URL checks: wrong short guesses (vladik, voronez, yekaterinburg)
can silently serve Moscow; synonyms encode known exceptions; otherwise we transliterate
Cyrillic to a latin slug (Voronеж -> voronezh, Ростов-на-Дону -> rostov-na-donu).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Optional

# Normalized keys: NFKC lower, "ё"->"е", "г." stripped, "-" spaces collapsed.
# Values: prefix as in https://www.afisha.ru/<slug>/restaurants/
_CITY_SYNONYMS: Dict[str, str] = {
    "москва": "msk",
    "moscow": "msk",
    "moskva": "msk",
    "санкт петербург": "spb",
    "санктпетербург": "spb",
    "петербург": "spb",
    "питер": "spb",
    "спб": "spb",
    "ст петербург": "spb",
    "st petersburg": "spb",
    "st. petersburg": "spb",
    "saint petersburg": "spb",
    "sankt peterburg": "spb",
    "sankt-peterburg": "spb",
    "воронеж": "voronezh",
    "владивосток": "vladivostok",
    "ростов на дону": "rostov-na-donu",
    "екатеринбург": "ekaterinburg",
    "yekaterinburg": "ekaterinburg",
    # Latin labels that must not use naive transliteration (Y -> Afisha 404/msk fallback)
    "voronezh": "voronezh",
    "vladivostok": "vladivostok",
    "rostov on don": "rostov-na-donu",
    "rostov-na-donu": "rostov-na-donu",
    "ekaterinburg": "ekaterinburg",
    "novosibirsk": "novosibirsk",
    "kazan": "kazan",
    "samara": "samara",
    "ufa": "ufa",
    "krasnoyarsk": "krasnoyarsk",
    "omsk": "omsk",
    "chelyabinsk": "chelyabinsk",
    "krasnodar": "krasnodar",
    "tula": "tula",
    "tver": "tver",
    "sochi": "sochi",
    "irkutsk": "irkutsk",
}


def _normalize_city_key(label: str) -> str:
    t = unicodedata.normalize("NFKC", (label or "").strip()).lower()
    t = t.replace("ё", "е")
    t = re.sub(r"^г\.?\s*", "", t)
    t = t.replace("-", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _transliterate_ru_word(word: str) -> str:
    t = word.lower().replace("ё", "e")
    # Longer clusters first
    replacements = [
        ("щ", "sch"),
        ("ш", "sh"),
        ("ч", "ch"),
        ("ж", "zh"),
        ("ю", "yu"),
        ("я", "ya"),
        ("й", "y"),
        ("ы", "y"),
        ("х", "h"),
        ("ц", "ts"),
        ("э", "e"),
        ("ъ", ""),
        ("ь", ""),
    ]
    for a, b in replacements:
        t = t.replace(a, b)
    single = str.maketrans(
        "абвгдезиклмнопрстуф",
        "abvgdeziklmnoprstuf",
    )
    t = t.translate(single)
    return t


def _transliterate_ru(label: str) -> str:
    """Label is Cyrillic (possibly multi-word)."""
    parts = re.split(r"[\s_\-]+", label.strip())
    lat: list[str] = []
    for p in parts:
        if not p:
            continue
        if re.fullmatch(r"[a-z][a-z0-9\-]*", p.lower()):
            lat.append(p.lower())
        elif re.search(r"[\u0400-\u04FF]", p):
            lat.append(_transliterate_ru_word(p))
        else:
            lat.append(p.lower())
    return "-".join(x for x in lat if x)


def _slugify_latin(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


# Human-readable city label for LLM / prompts (best-effort by Afisha slug).
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
    Return Afisha city path segment or None if *city* is empty after trim.
    """
    if city is None:
        return None
    raw = str(city).strip()
    if not raw:
        return None

    key = _normalize_city_key(raw)
    if not key:
        return None
    if key in _CITY_SYNONYMS:
        return _CITY_SYNONYMS[key]

    # Already latin slug-ish (e.g. user typed novosibirsk)
    if not re.search(r"[\u0400-\u04FF]", raw):
        slug = _slugify_latin(raw.replace(" ", "-"))
        if (
            slug
            and re.fullmatch(r"[a-z0-9\-]+", slug)
            and re.search(r"[a-z]", slug)
        ):
            return slug
        return None

    slug = _slugify_latin(_transliterate_ru(raw))
    if slug and re.search(r"[a-z]", slug):
        return slug
    return None
