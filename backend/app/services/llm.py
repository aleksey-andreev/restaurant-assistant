from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from openai import AsyncOpenAI
import httpx
import yaml


DEFAULT_BASE_URL = os.environ.get(
    "CLOUDRU_FOUNDATION_MODELS_URL",
    "https://foundation-models.api.cloud.ru/v1",
)
DEFAULT_API_KEY_ENV = "CLOUDRU_FOUNDATION_MODELS_API_KEY"
NODES_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config",
    "nodes.yaml",
)


class LLMClientError(Exception):
    pass


@dataclass
class LLMNodeConfig:
    name: str
    base_url: str
    api_key_env: str
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

    def __init__(self, api_key: str, base_url: str) -> None:
        timeout_raw = os.environ.get("LLM_HTTP_TIMEOUT_S", "45")
        try:
            timeout_s = max(10.0, float(timeout_raw))
        except (TypeError, ValueError):
            timeout_s = 45.0
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )

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
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        nodes: Dict[str, Tuple[AsyncLLMClient, str, Dict[str, Any]]] = {}
        for name, cfg in raw.get("nodes", {}).items():
            node_cfg = LLMNodeConfig(
                name=name,
                base_url=cfg.get("base_url") or DEFAULT_BASE_URL,
                api_key_env=cfg.get("api_key_env") or DEFAULT_API_KEY_ENV,
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

            api_key = os.environ.get(node_cfg.api_key_env)
            if not api_key:
                raise LLMClientError(
                    f"Environment variable {node_cfg.api_key_env} is not set"
                )

            client = AsyncLLMClient(api_key=api_key, base_url=node_cfg.base_url)
            params: Dict[str, Any] = {
                "model": node_cfg.model,
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

