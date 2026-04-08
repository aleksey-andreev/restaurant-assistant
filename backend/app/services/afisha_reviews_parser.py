from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

import httpx
from bs4 import BeautifulSoup


def _with_tab_param(url: str, tab_value: str) -> str:
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    q["tab"] = [tab_value]
    new_q = urlencode(q, doseq=True)
    return urlunparse(parsed._replace(query=new_q))


def extract_review_texts(html: str, *, max_reviews: int = 12) -> List[str]:
    """
    Afisha reviews markup may change. We use heuristics:
    - look for elements with class/id containing review/отзыв/comment
    - fallback to capturing paragraphs near the word "Отзывы"
    """
    soup = BeautifulSoup(html, "lxml")
    review_texts: List[str] = []

    # Heuristic 1: class-based extraction
    for el in soup.find_all(True):
        cls = " ".join(el.get("class", [])) if hasattr(el, "get") else ""
        el_id = str(el.get("id", "")) if hasattr(el, "get") else ""
        hay = f"{cls} {el_id}".lower()
        if any(k in hay for k in ["review", "otzyv", "comment", "otзыв"]):
            text = el.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 60 and len(text) <= 3000:
                review_texts.append(text)
        if len(review_texts) >= max_reviews:
            break

    # Heuristic 2: fallback from text around "Отзывы"
    if not review_texts:
        full_text = soup.get_text("\n", strip=True)
        # take small window after "Отзывы"
        idx = full_text.lower().find("отзывы")
        if idx >= 0:
            snippet = full_text[idx : idx + 6000]
            # split by blank lines and keep longer chunks
            chunks = [c.strip() for c in re.split(r"\n{2,}", snippet) if c.strip()]
            for c in chunks:
                if 60 <= len(c) <= 3000:
                    review_texts.append(c)
                if len(review_texts) >= max_reviews:
                    break

    # Dedupe preserve order
    seen = set()
    out: List[str] = []
    for t in review_texts:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:max_reviews]


async def fetch_and_extract_reviews(
    restaurant_url: str,
    *,
    timeout_s: float = 20.0,
    max_reviews: int = 12,
    headers: Optional[Dict[str, str]] = None,
) -> List[str]:
    tab_url = _with_tab_param(restaurant_url, "reviews")
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        resp = await client.get(tab_url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    return extract_review_texts(html, max_reviews=max_reviews)

