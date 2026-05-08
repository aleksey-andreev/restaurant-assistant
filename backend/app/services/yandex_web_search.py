from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


DEFAULT_YANDEX_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"


class YandexWebSearchError(Exception):
    pass


@dataclass(frozen=True)
class YandexSearchConfig:
    api_key_id: str
    api_key: str
    folder_id: str
    search_url: str
    search_type: str = "SEARCH_TYPE_RU"
    family_mode: str = "FAMILY_MODE_MODERATE"
    fix_typo_mode: str = "FIX_TYPO_MODE_ON"
    response_format: str = "FORMAT_XML"
    sort_mode: str = "SORT_MODE_BY_RELEVANCE"
    sort_order: str = "SORT_ORDER_DESC"
    user_agent: str = "restraunt-assistant/1.0"


def _build_auth_header(api_key: str) -> Dict[str, str]:
    """
    Yandex Search API expects only the API key secret in Authorization:
      Authorization: Api-Key <secret>
    """
    return {"Authorization": f"Api-Key {api_key}"}


def _decode_raw_data(raw_data_b64: str) -> str:
    try:
        decoded = base64.b64decode(raw_data_b64)
        return decoded.decode("utf-8", errors="ignore")
    except Exception as exc:  # pragma: no cover
        raise YandexWebSearchError("Failed to decode rawData (base64)") from exc


def _extract_urls_from_raw(
    raw_text: str,
    *,
    domain_allowlist: Optional[List[str]] = None,
    require_afisha_host: bool = True,
) -> List[str]:
    urls = re.findall(r"https?://[^\s\"'<>]+", raw_text)
    if not urls:
        return []

    out: List[str] = []
    for u in urls:
        if domain_allowlist:
            if not any(d in u for d in domain_allowlist):
                continue
        if require_afisha_host and "afisha.ru" not in u:
            continue
        out.append(u)

    # keep order but dedupe
    seen = set()
    unique: List[str] = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        unique.append(u)
    return unique


class YandexWebSearchClient:
    def __init__(self, config: YandexSearchConfig) -> None:
        self._cfg = config

    @classmethod
    def from_env(cls) -> "YandexWebSearchClient":
        api_key_id = os.environ.get("YANDEX_SEARCH_API_KEY_ID")
        api_key = os.environ.get("YANDEX_SEARCH_API_KEY")
        folder_id = os.environ.get("YANDEX_FOLDER_ID")
        missing = [k for k, v in {"YANDEX_SEARCH_API_KEY_ID": api_key_id, "YANDEX_SEARCH_API_KEY": api_key, "YANDEX_FOLDER_ID": folder_id}.items() if not v]
        if missing:
            raise YandexWebSearchError(f"Missing env vars for Yandex Search: {', '.join(missing)}")

        search_url = (
            os.environ.get("YANDEX_SEARCH_URL", DEFAULT_YANDEX_SEARCH_URL)
            or DEFAULT_YANDEX_SEARCH_URL
        ).rstrip("/")

        return cls(
            YandexSearchConfig(
                api_key_id=api_key_id or "",
                api_key=api_key or "",
                folder_id=folder_id or "",
                search_url=search_url,
            )
        )

    async def search(self, query_text: str, *, page: int = 0, max_docs: int = 50) -> List[str]:
        body: Dict[str, Any] = {
            "query": {
                "searchType": self._cfg.search_type,
                "queryText": query_text,
                "familyMode": self._cfg.family_mode,
                "page": str(page),
                "fixTypoMode": self._cfg.fix_typo_mode,
            },
            "sortSpec": {"sortMode": self._cfg.sort_mode, "sortOrder": self._cfg.sort_order},
            "maxPassages": "1",
            "folderId": self._cfg.folder_id,
            "responseFormat": self._cfg.response_format,
            "userAgent": self._cfg.user_agent,
        }

        headers = {
            "Content-Type": "application/json",
            **_build_auth_header(self._cfg.api_key),
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            resp = await client.post(self._cfg.search_url, headers=headers, json=body)
            if resp.status_code >= 400:
                raise YandexWebSearchError(
                    f"Yandex web search failed: status={resp.status_code} body={resp.text[:500]}"
                )

            payload = resp.json()
            raw_data_b64 = payload.get("rawData")
            if not raw_data_b64 or not isinstance(raw_data_b64, str):
                # Sometimes Yandex might respond with nested response.rawData depending on operation mode.
                raw_data_b64 = (
                    payload.get("response", {}).get("rawData") if isinstance(payload.get("response"), dict) else None
                )

            if not raw_data_b64 or not isinstance(raw_data_b64, str):
                return []

            raw_text = _decode_raw_data(raw_data_b64)
            urls = _extract_urls_from_raw(
                raw_text,
                domain_allowlist=["afisha.ru"],
                require_afisha_host=True,
            )
            return urls[:max_docs]

    async def search_raw_xml(self, query_text: str, *, page: int = 0) -> str:
        """Decoded Yandex Search XML/HTML body (base64 rawData). For parsing snippets, not Afisha URL extraction."""
        body: Dict[str, Any] = {
            "query": {
                "searchType": self._cfg.search_type,
                "queryText": query_text,
                "familyMode": self._cfg.family_mode,
                "page": str(page),
                "fixTypoMode": self._cfg.fix_typo_mode,
            },
            "sortSpec": {"sortMode": self._cfg.sort_mode, "sortOrder": self._cfg.sort_order},
            "maxPassages": "2",
            "folderId": self._cfg.folder_id,
            "responseFormat": self._cfg.response_format,
            "userAgent": self._cfg.user_agent,
        }
        headers = {
            "Content-Type": "application/json",
            **_build_auth_header(self._cfg.api_key),
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            resp = await client.post(self._cfg.search_url, headers=headers, json=body)
            if resp.status_code >= 400:
                raise YandexWebSearchError(
                    f"Yandex web search failed: status={resp.status_code} body={resp.text[:500]}"
                )
            payload = resp.json()
            raw_data_b64 = payload.get("rawData")
            if not raw_data_b64 or not isinstance(raw_data_b64, str):
                raw_data_b64 = (
                    payload.get("response", {}).get("rawData")
                    if isinstance(payload.get("response"), dict)
                    else None
                )
            if not raw_data_b64 or not isinstance(raw_data_b64, str):
                return ""
            return _decode_raw_data(raw_data_b64)

