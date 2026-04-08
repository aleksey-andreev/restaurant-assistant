from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from openai import AsyncOpenAI
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
    max_tokens: int
    temperature: float
    presence_penalty: float
    top_p: float
    system_prompt: str


class AsyncLLMClient:
    """
    Thin async wrapper over OpenAI-compatible chat.completions API.
    """

    def __init__(self, api_key: str, base_url: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(self, messages: Any, **params: Any) -> str:
        response = await self._client.chat.completions.create(
            messages=messages,
            **params,
        )
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
                max_tokens=int(cfg.get("max_tokens", 2500)),
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
                "max_tokens": node_cfg.max_tokens,
                "temperature": node_cfg.temperature,
                "presence_penalty": node_cfg.presence_penalty,
                "top_p": node_cfg.top_p,
            }

            nodes[name] = (client, node_cfg.system_prompt, params)

        return cls(nodes)

    def get_default_node(self) -> Tuple[AsyncLLMClient, str, Dict[str, Any]]:
        # For now, use the first configured node as the default dialog node.
        if not self._nodes:
            raise LLMClientError("No LLM nodes configured")
        return next(iter(self._nodes.values()))

