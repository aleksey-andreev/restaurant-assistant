from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

from dotenv import load_dotenv
from openai import AsyncOpenAI
import httpx
import yaml

logger = logging.getLogger(__name__)


LLM_PROVIDER_ENV = "LLM_PROVIDER"
DEFAULT_BASE_URL = os.environ.get(
    "CLOUDRU_FOUNDATION_MODELS_URL",
    "https://foundation-models.api.cloud.ru/v1",
)
NODES_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config",
    "nodes.yaml",
)

LlmProviderName = Literal["cloudru", "yandex"]


class LLMClientError(Exception):
    pass


@dataclass(frozen=True)
class LLMProviderSettings:
    provider: LlmProviderName
    api_key: str
    base_url: str
    project: Optional[str]
    model_prefix: str


def _parse_llm_provider(raw: Optional[str]) -> LlmProviderName:
    value = (raw or "cloudru").strip().lower()
    if value in ("cloudru", "cloud.ru"):
        return "cloudru"
    if value == "yandex":
        return "yandex"
    raise LLMClientError(
        f"{LLM_PROVIDER_ENV} must be CLOUDRU or YANDEX, got {raw!r}"
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LLMClientError(f"Environment variable {name} is not set")
    return value


def _load_project_env() -> None:
    """Pick up new keys from .env without full process restart (uvicorn reload ignores .env)."""
    project_root = Path(__file__).resolve().parents[3]
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)


def resolve_llm_provider_settings() -> LLMProviderSettings:
    _load_project_env()
    provider = _parse_llm_provider(os.environ.get(LLM_PROVIDER_ENV))
    if provider == "yandex":
        folder_id = _require_env("YANDEX_FOLDER_ID")
        settings = LLMProviderSettings(
            provider=provider,
            api_key=_require_env("YANDEX_CLOUD_API_KEY"),
            base_url=_require_env("YANDEX_CLOUD_URL"),
            project=folder_id,
            model_prefix=f"gpt://{folder_id}/",
        )
    else:
        settings = LLMProviderSettings(
            provider=provider,
            api_key=_require_env("CLOUDRU_FOUNDATION_MODELS_API_KEY"),
            base_url=os.environ.get("CLOUDRU_FOUNDATION_MODELS_URL", DEFAULT_BASE_URL).strip()
            or DEFAULT_BASE_URL,
            project=None,
            model_prefix="",
        )
    logger.info(
        "LLM provider=%s base_url=%s model_prefix=%r",
        settings.provider,
        settings.base_url,
        settings.model_prefix,
    )
    return settings


@dataclass
class LLMNodeConfig:
    name: str
    model: str
    max_tokens: Optional[int]
    temperature: float
    presence_penalty: float
    top_p: float
    system_prompt: str


class AsyncLLMClient:
    """
    Thin async wrapper over OpenAI-compatible chat.completions API.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        project: Optional[str] = None,
    ) -> None:
        timeout_raw = os.environ.get("LLM_HTTP_TIMEOUT_S", "45")
        try:
            timeout_s = max(10.0, float(timeout_raw))
        except (TypeError, ValueError):
            timeout_s = 45.0
        kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout": httpx.Timeout(timeout_s, connect=10.0),
        }
        if project:
            kwargs["project"] = project
        self._client = AsyncOpenAI(**kwargs)

    async def chat_completion(self, messages: Any, **params: Any) -> Any:
        return await self._client.chat.completions.create(
            messages=messages,
            **params,
        )

    async def chat(self, messages: Any, **params: Any) -> str:
        response = await self.chat_completion(messages, **params)
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:  # pragma: no cover - defensive
            raise LLMClientError("Invalid response from LLM provider") from exc


class LLMClientRegistry:
    """
    Loads node configurations from YAML and provides LLM clients and parameters
    for each node in the graph.
    """

    def __init__(self, nodes: Dict[str, Tuple[AsyncLLMClient, str, Dict[str, Any]]]):
        self._nodes = nodes

    @classmethod
    def from_config(cls, path: str = NODES_CONFIG_PATH) -> "LLMClientRegistry":
        settings = resolve_llm_provider_settings()
        client = AsyncLLMClient(
            api_key=settings.api_key,
            base_url=settings.base_url,
            project=settings.project,
        )

        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        nodes: Dict[str, Tuple[AsyncLLMClient, str, Dict[str, Any]]] = {}
        for name, cfg in raw.get("nodes", {}).items():
            node_cfg = LLMNodeConfig(
                name=name,
                model=cfg["model"],
                max_tokens=(
                    int(cfg["max_tokens"])
                    if cfg.get("max_tokens") is not None
                    else None
                ),
                temperature=float(cfg.get("temperature", 0.5)),
                presence_penalty=float(cfg.get("presence_penalty", 0.0)),
                top_p=float(cfg.get("top_p", 0.95)),
                system_prompt=cfg.get("system_prompt", ""),
            )

            params: Dict[str, Any] = {
                "model": settings.model_prefix + node_cfg.model,
                "temperature": node_cfg.temperature,
                "presence_penalty": node_cfg.presence_penalty,
                "top_p": node_cfg.top_p,
            }
            if node_cfg.max_tokens is not None:
                params["max_tokens"] = node_cfg.max_tokens

            nodes[name] = (client, node_cfg.system_prompt, params)

        return cls(nodes)

    def get_default_node(self) -> Tuple[AsyncLLMClient, str, Dict[str, Any]]:
        # Use the first configured node as the default dialog node.
        if not self._nodes:
            raise LLMClientError("No LLM nodes configured")
        return next(iter(self._nodes.values()))

    def get_node(self, name: str) -> Tuple[AsyncLLMClient, str, Dict[str, Any]]:
        """Return a specific node by name; raises LLMClientError if not found."""
        if name not in self._nodes:
            raise LLMClientError(f"LLM node '{name}' is not configured in nodes.yaml")
        return self._nodes[name]

    def get_node_or_default(self, name: str) -> Tuple[AsyncLLMClient, str, Dict[str, Any]]:
        """Return a specific node by name, falling back to default_dialog if not found."""
        if name in self._nodes:
            return self._nodes[name]
        return self.get_default_node()
