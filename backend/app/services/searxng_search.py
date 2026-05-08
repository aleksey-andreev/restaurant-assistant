from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import urlencode

import httpx


class SearxngSearchError(Exception):
    pass


@dataclass(frozen=True)
class SearxngConfig:
    base_url: str
    timeout_s: float = 20.0
    language: str = "ru"
    safesearch: int = 0
    format: str = "json"
    engines: str = ""
    user_agent: str = "restaurant-assistant/1.0"


class SearxngSearchClient:
    def __init__(self, cfg: SearxngConfig) -> None:
        self._cfg = cfg

    @classmethod
    def from_env(cls) -> "SearxngSearchClient":
        base_url = (os.environ.get("SEARXNG_BASE_URL") or "").strip().rstrip("/")
        if not base_url:
            raise SearxngSearchError("Missing env var: SEARXNG_BASE_URL")
        timeout_raw = os.environ.get("SEARXNG_TIMEOUT_S", "20")
        try:
            timeout_s = max(5.0, float(timeout_raw))
        except (TypeError, ValueError):
            timeout_s = 20.0
        return cls(
            SearxngConfig(
                base_url=base_url,
                timeout_s=timeout_s,
                language=(os.environ.get("SEARXNG_LANGUAGE") or "ru").strip() or "ru",
                engines=(os.environ.get("SEARXNG_ENGINES") or "").strip(),
                user_agent=(os.environ.get("SEARXNG_USER_AGENT") or "restaurant-assistant/1.0").strip(),
            )
        )

    async def search_raw_xml(self, query_text: str, *, page: int = 0) -> str:
        """
        Compatibility shim for external_rating: returns a plain text blob built
        from SearXNG JSON results (not actual XML).
        """
        if not query_text.strip():
            return ""
        params: Dict[str, Any] = {
            "q": query_text,
            "format": self._cfg.format,
            "language": self._cfg.language,
            "safesearch": str(self._cfg.safesearch),
            "pageno": str(max(1, int(page) + 1)),
        }
        if self._cfg.engines:
            params["engines"] = self._cfg.engines
        url = f"{self._cfg.base_url}/search?{urlencode(params)}"
        headers = {"User-Agent": self._cfg.user_agent}
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._cfg.timeout_s), headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                raise SearxngSearchError(f"SearXNG search failed: status={resp.status_code} body={resp.text[:300]}")
            payload = resp.json()
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return ""
        lines: List[str] = [f"query: {query_text}"]
        for it in items[:30]:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            content = str(it.get("content") or "").strip()
            url_i = str(it.get("url") or "").strip()
            engine = str(it.get("engine") or "").strip()
            if title:
                lines.append(f"title: {title}")
            if content:
                lines.append(f"passage: {content}")
            if url_i:
                lines.append(f"url: {url_i}")
            if engine:
                lines.append(f"source: {engine}")
        return "\n".join(lines)

