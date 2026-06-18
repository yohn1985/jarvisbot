"""Reasoning + context resolver in jarvis.adapters.llm — the single source of truth for which control
each backend exposes and what gets applied. Pure functions, no network."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis.adapters import llm


class BackendKindTests(unittest.TestCase):
    def test_cli_backends(self):
        self.assertEqual(llm.backend_kind("claude", ["claude", "-p"]), "claude_cli")
        self.assertEqual(llm.backend_kind("codex", ["codex", "exec"]), "codex_cli")
        self.assertEqual(llm.backend_kind("ollama", ["ollama", "run"]), "ollama_local")

    def test_http_backends(self):
        self.assertEqual(llm.backend_kind("", {"http": "https://ollama.com/v1/chat/completions"}), "ollama_http")
        self.assertEqual(llm.backend_kind("", {"http": "https://api.openai.com/v1/chat/completions"}), "openai_http")
        self.assertEqual(llm.backend_kind("", {"http": "https://api.anthropic.com/v1/messages", "format": "anthropic"}),
                         "anthropic_http")


class ReasoningCapsTests(unittest.TestCase):
    def test_claude_cli_is_effort_with_xhigh_max(self):
        caps = llm.reasoning_caps("claude_cli")
        self.assertEqual(caps["control"], "effort")
        self.assertEqual(caps["options"], ["low", "medium", "high", "xhigh", "max"])

    def test_codex_and_openai_effort_set(self):
        for kind in ("codex_cli", "openai_http"):
            self.assertEqual(llm.reasoning_caps(kind), {"control": "effort", "options": ["minimal", "low", "medium", "high"]})

    def test_anthropic_http_is_thinking(self):
        self.assertEqual(llm.reasoning_caps("anthropic_http")["control"], "thinking")

    def test_ollama_effort_only_when_thinking_capable(self):
        self.assertEqual(llm.reasoning_caps("ollama_http", ["completion", "tools", "thinking"])["control"], "effort")
        self.assertIsNone(llm.reasoning_caps("ollama_http", ["completion", "tools"])["control"])


class ReasoningValueTests(unittest.TestCase):
    def test_explicit_effort_kept_when_valid(self):
        self.assertEqual(llm.reasoning_value("claude_cli", {"effort": "xhigh"}), "xhigh")

    def test_generic_thinking_hint_maps_to_effort(self):
        # the chat passes think='medium' as a 'thinking' param; it must reach effort-based backends
        self.assertEqual(llm.reasoning_value("claude_cli", {"thinking": "medium"}), "medium")
        self.assertEqual(llm.reasoning_value("codex_cli", {"thinking": "high"}), "high")

    def test_out_of_set_value_dropped(self):
        self.assertEqual(llm.reasoning_value("claude_cli", {"effort": "minimal"}), "")   # claude has no 'minimal'
        self.assertEqual(llm.reasoning_value("codex_cli", {"effort": "xhigh"}), "")      # codex has no 'xhigh'

    def test_ollama_needs_thinking_caps_unless_forced(self):
        self.assertEqual(llm.reasoning_value("ollama_http", {"effort": "high"}, model_caps=["tools"]), "")
        self.assertEqual(llm.reasoning_value("ollama_http", {"effort": "high"}, model_caps=["thinking"]), "high")

    def test_default_and_blank_are_empty(self):
        self.assertEqual(llm.reasoning_value("claude_cli", {"effort": "default"}), "")
        self.assertEqual(llm.reasoning_value("claude_cli", {}), "")


class ReasoningDeltaTests(unittest.TestCase):
    def test_openai_compatible_reasoning_fields_are_real_thought(self):
        for key in ("reasoning_content", "reasoning", "thinking", "thinking_content"):
            with self.subTest(key=key):
                self.assertEqual(llm.reasoning_delta_text({key: "model thought"}), "model thought")

    def test_non_reasoning_delta_is_not_thought(self):
        self.assertEqual(llm.reasoning_delta_text({"content": "answer text"}), "")


class ContextOptionTests(unittest.TestCase):
    def test_claude_sonnet_opus_fable_get_1m(self):
        for m in ("claude-sonnet-4-6", "claude-opus-4-8", "claude-fable-5"):
            self.assertEqual(llm.context_options("claude_cli", 200000, m), [200000, 1000000])

    def test_claude_haiku_has_no_1m(self):
        self.assertEqual(llm.context_options("claude_cli", 200000, "claude-haiku-4-5"), [200000])

    def test_ollama_offers_sizes_up_to_max(self):
        opts = llm.context_options("ollama_http", 524288, "deepseek-v4-pro")
        self.assertEqual(opts[-1], 524288)
        self.assertTrue(all(o <= 524288 for o in opts))
        self.assertGreater(len(opts), 1)

    def test_codex_single_fixed_window(self):
        self.assertEqual(llm.context_options("codex_cli", 400000, "gpt-5.5"), [400000])


class ClaudeVariantTests(unittest.TestCase):
    def test_1m_appends_suffix_for_capable_models(self):
        self.assertEqual(llm.claude_model_variant("claude-sonnet-4-6", {"context": 1000000}), "claude-sonnet-4-6[1m]")

    def test_standard_context_no_suffix(self):
        self.assertEqual(llm.claude_model_variant("claude-sonnet-4-6", {"context": 200000}), "claude-sonnet-4-6")

    def test_haiku_never_gets_1m(self):
        self.assertEqual(llm.claude_model_variant("claude-haiku-4-5", {"context": 1000000}), "claude-haiku-4-5")

    def test_num_ctx_only_for_ollama(self):
        self.assertEqual(llm.context_num_ctx("ollama_http", {"context": 131072}), 131072)
        self.assertEqual(llm.context_num_ctx("claude_cli", {"context": 131072}), 0)


class OneMFlagTests(unittest.TestCase):
    def test_mark_clear_roundtrip(self):
        llm.clear_1m_unavailable()
        self.assertFalse(llm.one_m_unavailable())
        llm.mark_1m_unavailable()
        self.assertTrue(llm.one_m_unavailable())
        llm.clear_1m_unavailable()
        self.assertFalse(llm.one_m_unavailable())


class CodexInvokeTests(unittest.TestCase):
    def test_codex_invocation_uses_canonical_noninteractive_flags(self):
        router = llm.RoutingLLM(
            routing={},
            backends={"codex": ["codex", "exec", "--model", "{model}", "{prompt}"]},
            aliases={},
            fallbacks=[],
        )

        def fake_run(cmd, input=None, capture_output=False, text=False, timeout=None):
            self.assertEqual(cmd[:4], ["codex", "exec", "--model", "gpt-5.5"])
            self.assertIn("--cd", cmd)
            self.assertIn("--sandbox", cmd)
            self.assertIn("danger-full-access", cmd)
            self.assertIn('approval_policy="never"', cmd)
            self.assertIn("--skip-git-repo-check", cmd)
            self.assertIn("--output-last-message", cmd)
            self.assertNotIn("Reply exactly OK", cmd)
            self.assertEqual(input, "Reply exactly OK")
            final = Path(cmd[cmd.index("--output-last-message") + 1])
            final.write_text("OK")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("jarvis.adapters.llm.subprocess.run", side_effect=fake_run):
            self.assertEqual(router._invoke("codex", "gpt-5.5", "Reply exactly OK", 30), "OK")


if __name__ == "__main__":
    unittest.main()
