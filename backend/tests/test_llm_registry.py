"""LLM registry config loading."""

from __future__ import annotations

import os
import unittest
from unittest.mock import mock_open, patch

from app.services.llm import (
    LLMClientError,
    LLMClientRegistry,
    NODES_CONFIG_PATH,
    resolve_llm_provider_settings,
)

_NODES_YAML = """
nodes:
  default_dialog:
    model: "gpt-oss-120b/latest"
    max_tokens: 10000
    temperature: 0.5
    presence_penalty: 0.0
    top_p: 0.95
    system_prompt: "sys"
  requirements_elicitation:
    model: "gpt-oss-120b/latest"
    max_tokens: 10000
    temperature: 0.2
    presence_penalty: 0.0
    top_p: 0.95
    system_prompt: "sys"
"""


class TestResolveLlmProviderSettings(unittest.TestCase):
    def setUp(self) -> None:
        self._env_loader = patch("app.services.llm._load_project_env")
        self._env_loader.start()

    def tearDown(self) -> None:
        self._env_loader.stop()

    def test_yandex_model_prefix(self) -> None:
        env = {
            "LLM_PROVIDER": "YANDEX",
            "YANDEX_CLOUD_API_KEY": "key",
            "YANDEX_CLOUD_URL": "https://ai.api.cloud.yandex.net/v1",
            "YANDEX_FOLDER_ID": "b1gtest",
        }
        with patch.dict(os.environ, env, clear=True):
            s = resolve_llm_provider_settings()
        self.assertEqual(s.provider, "yandex")
        self.assertEqual(s.model_prefix, "gpt://b1gtest/")
        self.assertEqual(s.project, "b1gtest")

    def test_cloudru_empty_prefix(self) -> None:
        env = {
            "LLM_PROVIDER": "CLOUDRU",
            "CLOUDRU_FOUNDATION_MODELS_API_KEY": "key",
            "CLOUDRU_FOUNDATION_MODELS_URL": "https://foundation-models.api.cloud.ru/v1",
        }
        with patch.dict(os.environ, env, clear=True):
            s = resolve_llm_provider_settings()
        self.assertEqual(s.provider, "cloudru")
        self.assertEqual(s.model_prefix, "")
        self.assertIsNone(s.project)

    def test_default_provider_is_cloudru(self) -> None:
        env = {
            "CLOUDRU_FOUNDATION_MODELS_API_KEY": "key",
        }
        with patch.dict(os.environ, env, clear=True):
            s = resolve_llm_provider_settings()
        self.assertEqual(s.provider, "cloudru")

    def test_yandex_missing_folder_raises(self) -> None:
        env = {
            "LLM_PROVIDER": "YANDEX",
            "YANDEX_CLOUD_API_KEY": "key",
            "YANDEX_CLOUD_URL": "https://ai.api.cloud.yandex.net/v1",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(LLMClientError):
                resolve_llm_provider_settings()


class TestLLMRegistryFromConfig(unittest.TestCase):
    def _registry(self, env: dict[str, str]) -> LLMClientRegistry:
        with patch("app.services.llm._load_project_env"):
            with patch.dict(os.environ, env, clear=True):
                with patch("builtins.open", mock_open(read_data=_NODES_YAML)):
                    return LLMClientRegistry.from_config(NODES_CONFIG_PATH)

    def test_yandex_model_uri_in_params(self) -> None:
        env = {
            "LLM_PROVIDER": "YANDEX",
            "YANDEX_CLOUD_API_KEY": "key",
            "YANDEX_CLOUD_URL": "https://ai.api.cloud.yandex.net/v1",
            "YANDEX_FOLDER_ID": "b1gtest",
        }
        reg = self._registry(env)
        _client, _sys, params = reg.get_node("requirements_elicitation")
        self.assertEqual(
            params.get("model"),
            "gpt://b1gtest/gpt-oss-120b/latest",
        )

    def test_cloudru_model_unchanged(self) -> None:
        env = {
            "LLM_PROVIDER": "CLOUDRU",
            "CLOUDRU_FOUNDATION_MODELS_API_KEY": "key",
            "CLOUDRU_FOUNDATION_MODELS_URL": "https://foundation-models.api.cloud.ru/v1",
        }
        reg = self._registry(env)
        _client, _sys, params = reg.get_node("requirements_elicitation")
        self.assertEqual(params.get("model"), "gpt-oss-120b/latest")

    def test_single_client_for_all_nodes(self) -> None:
        env = {
            "LLM_PROVIDER": "YANDEX",
            "YANDEX_CLOUD_API_KEY": "key",
            "YANDEX_CLOUD_URL": "https://ai.api.cloud.yandex.net/v1",
            "YANDEX_FOLDER_ID": "b1gtest",
        }
        reg = self._registry(env)
        c1, _, _ = reg.get_node("default_dialog")
        c2, _, _ = reg.get_node("requirements_elicitation")
        self.assertIs(c1, c2)


class TestLLMRegistryIntegration(unittest.TestCase):
    def test_requirements_elicitation_uses_configured_model(self) -> None:
        if os.environ.get("LLM_PROVIDER", "").strip().upper() == "YANDEX":
            if not os.environ.get("YANDEX_CLOUD_API_KEY"):
                self.skipTest("YANDEX_CLOUD_API_KEY not set")
        elif not os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY"):
            self.skipTest("CLOUDRU_FOUNDATION_MODELS_API_KEY not set")
        reg = LLMClientRegistry.from_config(NODES_CONFIG_PATH)
        _client, _sys, params = reg.get_node("requirements_elicitation")
        model = params.get("model") or ""
        self.assertIn("gpt-oss-120b", model)
        self.assertEqual(params.get("max_tokens"), 10000)
        self.assertAlmostEqual(float(params["temperature"]), 0.2)

    def test_default_dialog_has_max_tokens(self) -> None:
        if os.environ.get("LLM_PROVIDER", "").strip().upper() == "YANDEX":
            if not os.environ.get("YANDEX_CLOUD_API_KEY"):
                self.skipTest("YANDEX_CLOUD_API_KEY not set")
        elif not os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY"):
            self.skipTest("CLOUDRU_FOUNDATION_MODELS_API_KEY not set")
        reg = LLMClientRegistry.from_config(NODES_CONFIG_PATH)
        _client, _sys, params = reg.get_node("default_dialog")
        self.assertEqual(params.get("max_tokens"), 10000)

    def test_preorder_menu_pick(self) -> None:
        if os.environ.get("LLM_PROVIDER", "").strip().upper() == "YANDEX":
            if not os.environ.get("YANDEX_CLOUD_API_KEY"):
                self.skipTest("YANDEX_CLOUD_API_KEY not set")
        elif not os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY"):
            self.skipTest("CLOUDRU_FOUNDATION_MODELS_API_KEY not set")
        reg = LLMClientRegistry.from_config(NODES_CONFIG_PATH)
        _client, sys_prompt, params = reg.get_node("preorder_menu_pick")
        model = params.get("model") or ""
        self.assertIn("gpt-oss-120b", model)
        self.assertEqual(params.get("max_tokens"), 4096)
        self.assertAlmostEqual(float(params["temperature"]), 0.1)
        self.assertIn("items", sys_prompt)


if __name__ == "__main__":
    unittest.main()
