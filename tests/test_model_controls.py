import unittest

from jarvis.dashboard import server


class ModelControlTests(unittest.TestCase):
    def test_ollama_cloud_thinking_model_exposes_effort_not_thinking(self):
        spec = {"http": "https://ollama.com/v1/chat/completions", "api_key_env": "OLLAMA_API_KEY"}
        detected = {"context_window": 524288, "capabilities": ["completion", "tools", "thinking"]}

        controls = server._model_controls(
            "ollama_cloud",
            spec,
            "deepseek-v4-pro",
            detected,
            {"thinking": "medium"},
        )
        effective, unsupported = server._effective_reasoning_params({"thinking": "medium"}, controls)

        self.assertTrue(controls["effort"]["supported"])
        self.assertEqual(controls["effort"]["options"], ["default", "low", "medium", "high"])
        self.assertNotIn("none", controls["effort"]["options"])  # 'none' is not a real reasoning_effort
        self.assertEqual(controls["effort"]["value"], "default")
        self.assertFalse(controls["thinking"]["supported"])
        self.assertEqual(effective, {})
        self.assertEqual(unsupported, {"thinking": "medium"})

    def test_ollama_cloud_non_thinking_model_has_no_reasoning_controls(self):
        spec = {"http": "https://ollama.com/v1/chat/completions", "api_key_env": "OLLAMA_API_KEY"}
        detected = {"context_window": 524288, "capabilities": ["completion"]}

        controls = server._model_controls("ollama_cloud", spec, "gemma3:4b", detected, {})

        self.assertFalse(controls["effort"]["supported"])
        self.assertFalse(controls["thinking"]["supported"])

    def test_claude_cli_exposes_effort_not_thinking(self):
        # The Claude CLI controls reasoning via the native --effort flag (low..max), NOT a thinking budget.
        controls = server._model_controls("claude", ["claude", "-p"], "claude-opus-4-8", {}, {"effort": "max"})

        self.assertTrue(controls["effort"]["supported"])
        self.assertEqual(controls["effort"]["options"], ["default", "low", "medium", "high", "xhigh", "max"])
        self.assertEqual(controls["effort"]["value"], "max")
        self.assertFalse(controls["thinking"]["supported"])

    def test_sanitize_removes_stale_thinking_for_ollama_cloud(self):
        old_context_window = server._context_window
        try:
            server._context_window = lambda pool, spec, model: {
                "context_window": 524288,
                "capabilities": ["completion", "tools", "thinking"],
            }
            data = {
                "llm": {
                    "backends": {
                        "ollama_cloud": {
                            "http": "https://ollama.com/v1/chat/completions",
                            "api_key_env": "OLLAMA_API_KEY",
                        }
                    },
                    "routing": {"orchestrator": "ollama_cloud:deepseek-v4-pro"},
                    "params": {"orchestrator": {"thinking": "medium", "effort": "high"}},
                }
            }

            changed = server._sanitize_llm_params(data)
        finally:
            server._context_window = old_context_window

        self.assertEqual(data["llm"]["params"], {"orchestrator": {"effort": "high"}})
        self.assertIn("params.orchestrator.thinking", changed)


if __name__ == "__main__":
    unittest.main()
