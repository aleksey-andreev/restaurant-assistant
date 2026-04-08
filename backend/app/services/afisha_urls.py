from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from typing import List, Optional


def _host_lc(netloc: str) -> str:
    h = (netloc or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def _is_afisha_host(host: str) -> bool:
    return host == "afisha.ru" or host.endswith(".afisha.ru")


def _path_has_restaurant(path: str) -> bool:
    return "/restaurant/" in (path or "")


def _canonical_afisha_card_url(path: str) -> str:
    p = path or ""
    if not p.startswith("/"):
        p = "/" + p
    p = p.rstrip("/")
    if not p:
        p = "/"
    return urlunparse(("https", "www.afisha.ru", p, "", "", ""))


def _normalize_direct_only(raw: str) -> Optional[str]:
    """
    If *raw* is a direct Afisha restaurant card URL in SERP, return canonical form.
    Cache wrappers and other hosts return None — only real Afisha links count.
    """
    s = (raw or "").strip()
    if not s:
        return None
    s = s.split("#", 1)[0].replace("&amp;", "&")

    try:
        parsed = urlparse(s)
    except Exception:
        return None

    host = _host_lc(parsed.netloc)
    if not _is_afisha_host(host) or not _path_has_restaurant(parsed.path):
        return None

    return _canonical_afisha_card_url(parsed.path)


def filter_and_order_afisha_restaurant_urls(urls: List[str]) -> List[str]:
    """
    Keep only direct https://www.afisha.ru/.../restaurant/... links from the list.
    Preserve first-seen order; deduplicate.
    """
    out: List[str] = []
    seen: set[str] = set()
    for raw in urls:
        canon = _normalize_direct_only(raw)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
    return out
