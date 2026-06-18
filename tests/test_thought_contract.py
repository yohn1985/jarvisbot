"""Durable guard for the chat Thought contract (regression: a tab once showed harness EVIDENCE as the
model's Thought). Two layers: the reasoning-capture helper, and source-contract checks that the render
rule + per-backend capture can't silently regress. Enforced by the fitness/pre-push/CI gates."""
import re
import unittest
from pathlib import Path

from jarvis.adapters import llm

ROOT = Path(__file__).resolve().parent.parent
DASH = (ROOT / "jarvis" / "dashboard" / "dashboard.html").read_text()
LLM = (ROOT / "jarvis" / "adapters" / "llm.py").read_text()


class ReasoningDeltaTests(unittest.TestCase):
    def test_each_reasoning_field_captured(self):
        for field in ("reasoning_content", "reasoning", "thinking", "thinking_content"):
            self.assertEqual(llm.reasoning_delta_text({field: "why"}), "why", field)

    def test_non_string_or_empty_ignored(self):
        self.assertEqual(llm.reasoning_delta_text({}), "")
        self.assertEqual(llm.reasoning_delta_text({"reasoning": ""}), "")
        self.assertEqual(llm.reasoning_delta_text({"reasoning": {"x": 1}}), "")
        self.assertEqual(llm.reasoning_delta_text({"content": "answer"}), "")  # answer text is NOT reasoning


class RenderContractTests(unittest.TestCase):
    """The completed Thought block must come from real model reasoning only — never harness evidence."""

    def test_thought_renders_from_thinking(self):
        self.assertIn("visibleThinking(thinking)", DASH)

    def test_no_evidence_fallback_for_thought(self):
        self.assertNotIn("evidenceThought", DASH)              # the removed fake-Thought helper
        self.assertNotIn("thinking||evidence", DASH.replace(" ", ""))   # the removed fallback expression


class CaptureContractTests(unittest.TestCase):
    """Every streaming backend that can emit reasoning must route it into on_delta('thinking')."""

    def test_claude_cli_captures_thinking_delta(self):
        self.assertIn("thinking_delta", LLM)
        self.assertTrue(re.search(r'on_delta\(\s*["\']thinking["\']', LLM))

    def test_http_stream_uses_reasoning_helper(self):
        self.assertIn("reasoning_delta_text(delta)", LLM)


if __name__ == "__main__":
    unittest.main()
