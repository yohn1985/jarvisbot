import tempfile
import unittest
import json
from pathlib import Path

from jarvis import chat_tools, harness, local_knowledge


class FakeLLM:
    def __init__(self):
        self.prompt = ""

    def run(self, role, prompt, timeout=120):
        self.prompt = prompt
        return "Recovered answer from executed evidence."


class ChatToolRecoveryTests(unittest.TestCase):
    def test_extracts_nested_deepseek_tool_call(self):
        text = (
            '<tool_calls><tool_call name="read_file">'
            "<path>/repo/docs/servers.md</path>"
            "</tool_call></tool_calls>"
        )

        self.assertEqual(
            chat_tools.extract_xml_tool_calls(text),
            [{"tool": "read", "args": {"path": "/repo/docs/servers.md"}}],
        )

    def test_extracts_anthropic_invoke_parameters(self):
        text = (
            '<function_calls><invoke name="grep">'
            '<parameter name="pattern">pipeline</parameter>'
            '<parameter name="path">/repo/docs</parameter>'
            "</invoke></function_calls>"
        )

        self.assertEqual(
            chat_tools.extract_xml_tool_calls(text),
            [{"tool": "search", "args": {"pattern": "pipeline", "path": "/repo/docs"}}],
        )

    def test_keeps_flat_attribute_form_working(self):
        text = '<shell cmd="uptime" />'

        self.assertEqual(
            chat_tools.extract_xml_tool_calls(text),
            [{"tool": "shell", "args": {"cmd": "uptime"}}],
        )

    def test_repair_executes_nested_tool_call_and_summarizes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.txt"
            path.write_text("jarvis nested tool evidence")
            llm = FakeLLM()
            thinking = f'<tool_calls><tool_call name="read_file"><path>{path}</path></tool_call></tool_calls>'

            out = harness.repair_unexecuted_command_plan(llm, "read that file", "", thinking)

        self.assertEqual(out, "Recovered answer from executed evidence.")
        self.assertIn("jarvis nested tool evidence", llm.prompt)

    def test_extracts_json_tool_call(self):
        # The format DeepSeek/OpenAI-style models actually emit in content — previously dropped,
        # which ended the turn with no answer (issue #3).
        text = '{"tool":"shell","args":{"cmd":"uptime"}}'

        self.assertEqual(
            chat_tools.extract_json_tool_calls(text),
            [{"tool": "shell", "args": {"cmd": "uptime"}}],
        )

    def test_json_tool_call_ignores_non_tool_objects(self):
        self.assertEqual(chat_tools.extract_json_tool_calls('{"foo":1,"bar":{"x":2}}'), [])

    def test_aliases_run_command_to_shell(self):
        # The model invents tool names every turn (run_command, read_file, ...). Alias them instead
        # of dropping the call (issue #9).
        self.assertEqual(
            chat_tools.extract_json_tool_calls('{"tool":"run_command","args":{"command":"uptime"}}'),
            [{"tool": "shell", "args": {"cmd": "uptime"}}],
        )
        self.assertEqual(
            chat_tools.extract_xml_tool_calls('<tool_call name="run_command"><command>uptime</command></tool_call>'),
            [{"tool": "shell", "args": {"cmd": "uptime"}}],
        )
        self.assertEqual(
            chat_tools.extract_json_tool_calls('{"tool":"read_file","args":{"path":"README.md"}}'),
            [{"tool": "read", "args": {"path": "README.md"}}],
        )

    def test_normalize_native_tool_call(self):
        self.assertEqual(
            chat_tools.normalize_tool_call("shell", {"cmd": "uptime"}),
            {"tool": "shell", "args": {"cmd": "uptime"}},
        )
        self.assertIsNone(chat_tools.normalize_tool_call("definitely_not_a_tool", {"x": 1}))

    def test_run_tool_calls_executes_native(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.txt"
            path.write_text("native tool evidence")
            ev = harness.run_tool_calls([{"tool": "read", "args": {"path": str(path)}}], "read it")
        self.assertIn("native tool evidence", ev)

    def test_execute_recovered_runs_json_tool_call(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.txt"
            path.write_text("jarvis json tool evidence")
            text = '{"tool":"read","args":{"path":"%s"}}' % path

            ev = harness.execute_recovered_tool_calls(text, "read that file", limit=1)

        self.assertIn("jarvis json tool evidence", ev)

    def test_execution_profile_daily_by_default(self):
        profile = harness.select_execution_profile("explain the cognitive loop idea", "")

        self.assertEqual(profile["mode"], "daily")
        self.assertIn("low-risk", profile["reason"])

    def test_execution_profile_avoids_substring_false_positive(self):
        profile = harness.select_execution_profile("summarize the dashboard page", "")

        self.assertEqual(profile["mode"], "daily")

    def test_execution_profile_heavy_for_live_status(self):
        profile = harness.select_execution_profile("is the Jarvis service running right now?", "")

        self.assertEqual(profile["mode"], "heavy")
        self.assertIn("live", " ".join(profile["required_checks"]))

    def test_execution_profile_heavy_for_memory_must_verify(self):
        docs = "Learned memories relevant to this question:\n\nMEMORY: service X is running\nTRUST_POLICY: must_verify_before_answer\nVOLATILITY: volatile\nSTALE: no"

        profile = harness.select_execution_profile("what do you remember about service X?", docs)

        self.assertEqual(profile["mode"], "heavy")
        self.assertIn("recalled memory requires verification", profile["reason"])

    def test_fast_local_answer_returns_hint_for_non_direct_memory(self):
        docs = "Learned memories relevant to this question:\n\nMEMORY: the WhatsApp fix is deployed\nTRUST_POLICY: must_verify_before_answer\nVOLATILITY: volatile\nSTALE: no"

        answer = harness.fast_local_answer("do you remember the WhatsApp fix?", docs)

        self.assertIn("Memory hint:", answer)
        self.assertIn("verify", answer)

    def test_verification_evidence_pack_keeps_relevant_tail_command(self):
        evidence = (
            "$ systemctl list-timers --all --no-pager\n"
            + ("irrelevant timer output\n" * 500)
            + "\n$ systemctl is-active jarvis-dashboard.service\nactive\n"
        )

        pack = harness._verification_evidence_pack(
            "is the jarvis-dashboard.service running right now?",
            "`systemctl is-active jarvis-dashboard.service` returned `active`.",
            evidence,
            limit=3000,
        )

        self.assertLessEqual(len(pack), 3000)
        self.assertIn("$ systemctl is-active jarvis-dashboard.service", pack)
        self.assertIn("active", pack)

    def test_record_learned_memory_adds_hint_metadata_and_redacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "knowledge"
            old = {
                "KNOW_DIR": local_knowledge.KNOW_DIR,
                "DOC_INDEX": local_knowledge.DOC_INDEX,
                "CODE_INDEX": local_knowledge.CODE_INDEX,
                "OPS_INDEX": local_knowledge.OPS_INDEX,
                "LEARNED": local_knowledge.LEARNED,
                "LEARNED_DIR": local_knowledge.LEARNED_DIR,
            }
            try:
                local_knowledge.KNOW_DIR = root
                local_knowledge.DOC_INDEX = root / "local-docs-index.json"
                local_knowledge.CODE_INDEX = root / "local-code-index.json"
                local_knowledge.OPS_INDEX = root / "local-ops-index.json"
                local_knowledge.LEARNED = root / "learned.jsonl"
                local_knowledge.LEARNED_DIR = root / "learned"

                ok = local_knowledge.record_learned_memory(
                    "The WhatsApp fix is currently deployed",
                    source="tool",
                    evidence="token=super-secret-value",
                )

                row = json.loads(local_knowledge.LEARNED.read_text().splitlines()[0])
            finally:
                for name, value in old.items():
                    setattr(local_knowledge, name, value)

        self.assertTrue(ok)
        self.assertEqual(row["trust_policy"], "must_verify_before_answer")
        self.assertEqual(row["volatility"], "volatile")
        self.assertEqual(row["kind"], "volatile_status")
        self.assertIn("[redacted-secret]", row["evidence"])


if __name__ == "__main__":
    unittest.main()
