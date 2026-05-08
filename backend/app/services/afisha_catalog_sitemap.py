"""
Discover all Afisha restaurant card URLs for a city via official sitemaps.

Flow:
  https://www.afisha.ru/{city_slug}/restaurants/sitemap.xml  (index)
    -> …/sitemap-restuarants.xml  (typo preserved by Afisha; urlset of card URLs)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List
from urllib.parse import urlparse

import httpx

from .afisha_urls import filter_and_order_afisha_restaurant_urls


_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _pick_restaurant_leaf_sitemaps(index_xml: bytes, city_slug: str) -> List[str]:
    root = ET.fromstring(index_xml)
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    out: List[str] = []
    if tag != "sitemapindex":
        return out
    prefix = f"https://www.afisha.ru/{city_slug}/restaurants/"
    for loc in root.findall("sm:sitemap/sm:loc", _NS):
        u = (loc.text or "").strip()
        if not u.startswith(prefix):
            continue
        path = (urlparse(u).path or "").lower()
        # Afisha uses typo "restuarants" in the main card urlset filename.
        if path.endswith("/sitemap-restuarants.xml") or path.endswith("/sitemap-restaurants.xml"):
            out.append(u)
    return out


def _extract_locs_from_urlset(urlset_xml: bytes) -> List[str]:
    root = ET.fromstring(urlset_xml)
    return [loc.text.strip() for loc in root.findall(".//sm:loc", _NS) if loc.text and loc.text.strip()]


async def fetch_restaurant_urls_for_city(
    city_slug: str,
    *,
    http_timeout_s: float = 120.0,
    user_agent: str = "restaurant-assistant-catalog/1.0",
) -> List[str]:
    """
    Return canonical unique restaurant card URLs for *city_slug* (Afisha path prefix).
    """
    headers = {"User-Agent": user_agent, "Accept-Language": "ru-RU,ru;q=0.9"}
    index_url = f"https://www.afisha.ru/{city_slug}/restaurants/sitemap.xml"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(http_timeout_s),
        headers=headers,
        follow_redirects=True,
    ) as client:
        ir = await client.get(index_url)
        ir.raise_for_status()
        leaf_urls = _pick_restaurant_leaf_sitemaps(ir.content, city_slug)
        if not leaf_urls:
            raise ValueError(
                f"No restaurant leaf sitemap under {index_url!r} — check city_slug or Afisha layout."
            )

        all_raw: List[str] = []
        for leaf in leaf_urls:
            lr = await client.get(leaf)
            lr.raise_for_status()
            all_raw.extend(_extract_locs_from_urlset(lr.content))

    return filter_and_order_afisha_restaurant_urls(all_raw)
