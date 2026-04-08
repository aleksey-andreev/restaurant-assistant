from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import FrozenSet, Iterable, List, Tuple

_RULES_CACHE: Tuple[Tuple[str, Tuple[str, ...]], ...] | None = None


def _default_rules_path() -> Path:
    # backend/app/services/cuisine_normalize.py -> parents[2] == backend/
    return Path(__file__).resolve().parents[2] / "config" / "cuisine_rules.json"


def _load_cuisine_rules() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    env_path = os.environ.get("CUISINE_RULES_PATH")
    path = Path(env_path) if env_path else _default_rules_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Cuisine rules not found: {path}. Set CUISINE_RULES_PATH or add backend/config/cuisine_rules.json"
        )

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rules: List[Tuple[str, Tuple[str, ...]]] = []
    for item in data.get("cuisines", []):
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        kws = item.get("keywords", [])
        if not cid or not isinstance(kws, list):
            continue
        keywords = tuple(str(k).strip() for k in kws if str(k).strip())
        if keywords:
            rules.append((str(cid), keywords))

    _RULES_CACHE = tuple(rules)
    return _RULES_CACHE


def reload_cuisine_rules() -> None:
    """Clear cache (e.g. after hot-reload in dev)."""
    global _RULES_CACHE
    _RULES_CACHE = None


def normalize_label_key(label: str) -> str:
    s = label.strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", s)


def canonicals_for_label(label: str) -> FrozenSet[str]:
    """Map one Afisha/user label to zero or more canonical cuisine ids."""
    t = normalize_label_key(label)
    if not t:
        return frozenset()
    out: set[str] = set()
    for canon, keywords in _load_cuisine_rules():
        for kw in keywords:
            if kw.lower() in t:
                out.add(canon)
                break
    return frozenset(out)


def canonical_set_from_tags(tags: Iterable[str]) -> FrozenSet[str]:
    s: set[str] = set()
    for tag in tags:
        s.update(canonicals_for_label(str(tag)))
    return frozenset(s)


def canonical_set_from_phrases(phrases: Iterable[str]) -> FrozenSet[str]:
    """Same as tags — user free-form strings."""
    return canonical_set_from_tags(phrases)


def merge_tag_lists(primary: List[str], extra: Iterable[str]) -> List[str]:
    """Deduped append preserving order; for merging HTML tags + JSON-LD."""
    seen: set[str] = set()
    out: List[str] = []
    for t in primary:
        k = normalize_label_key(t)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(t.strip())
    for t in extra:
        k = normalize_label_key(str(t))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(str(t).strip())
    return out[:25]
