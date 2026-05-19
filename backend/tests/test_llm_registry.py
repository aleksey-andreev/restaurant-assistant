"""LLM registry config loading."""

from __future__ import annotations

import os
import unittest

from app.services.llm import LLMClientRegistry, NODES_CONFIG_PATH


class TestLLMRegistry(unittest.TestCase):
    def test_requirements_elicitation_uses_glm(self) -> None:
        if not os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY"):
            self.skipTest("CLOUDRU_FOUNDATION_MODELS_API_KEY not set")
        reg = LLMClientRegistry.from_config(NODES_CONFIG_PATH)
        _client, _sys, params = reg.get_node("requirements_elicitation")
        self.assertEqual(params.get("model"), "zai-org/GLM-4.7")
        self.assertEqual(params.get("max_tokens"), 10000)
        self.assertAlmostEqual(float(params["temperature"]), 0.2)

    def test_default_dialog_has_max_tokens(self) -> None:
        if not os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY"):
            self.skipTest("CLOUDRU_FOUNDATION_MODELS_API_KEY not set")
        reg = LLMClientRegistry.from_config(NODES_CONFIG_PATH)
        _client, _sys, params = reg.get_node("default_dialog")
        self.assertEqual(params.get("max_tokens"), 10000)

    def test_preorder_menu_pick_uses_glm(self) -> None:
        if not os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY"):
            self.skipTest("CLOUDRU_FOUNDATION_MODELS_API_KEY not set")
        reg = LLMClientRegistry.from_config(NODES_CONFIG_PATH)
        _client, sys_prompt, params = reg.get_node("preorder_menu_pick")
        self.assertEqual(params.get("model"), "zai-org/GLM-4.7")
        self.assertEqual(params.get("max_tokens"), 4096)
        self.assertAlmostEqual(float(params["temperature"]), 0.1)
        self.assertIn("items", sys_prompt)


if __name__ == "__main__":
    unittest.main()
