import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from jarvis import chat_tools, harness, local_knowledge


class FakeLLM:
    def __init__(self):
        self.prompt = ""

    def run(self, role, prompt, timeout=120):
        self.prompt = prompt
        return "Recovered answer from executed evidence."


class RoleFallbackLLM:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def run(self, role, prompt, timeout=120):
        self.calls.append(role)
        self.prompt = prompt
        value = self.responses.get(role, "")
        if isinstance(value, Exception):
            raise value
        return value


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

    def test_execution_profile_heavy_for_ticket_count_question(self):
        profile = harness.select_execution_profile("why did the open ticket go from 176 to 192?", "")

        self.assertEqual(profile["mode"], "heavy")
        self.assertIn("live", " ".join(profile["required_checks"]))

    def test_ticket_questions_start_with_gitea_evidence(self):
        calls = harness.planned_tool_calls("why did the open ticket go from 176 to 192?")

        self.assertEqual(len(calls), 1)
        self.assertTrue(all(call["tool"] == "shell" for call in calls))
        self.assertTrue(all(chat_tools.shell_safety_error(call["args"]["cmd"]) is None for call in calls))
        self.assertIn("jarvis.gitea_tools open-summary", calls[0]["args"]["cmd"])
        self.assertTrue(harness.planned_calls_are_sufficient("why did the open ticket go from 176 to 192?", calls))

    def test_explicit_ticket_number_searches_all_gitea_repos(self):
        calls = harness.planned_tool_calls("ticket number 34 pull it up")

        self.assertEqual(len(calls), 1)
        cmd = calls[0]["args"]["cmd"]
        self.assertIn("jarvis.gitea_tools issue 34", cmd)
        self.assertIsNone(chat_tools.shell_safety_error(cmd))
        self.assertTrue(harness.planned_calls_are_sufficient("ticket number 34 pull it up", calls))

    def test_screenshot_url_is_planned_as_show_image(self):
        calls = harness.planned_tool_calls(
            "https://screenshot.example.com/screenshots/2026/06/17/example.png"
        )

        self.assertEqual(calls[0]["tool"], "show_image")
        self.assertIn("https://screenshot.example.com/", calls[0]["args"]["path"])
        self.assertTrue(harness.planned_calls_are_sufficient("show this screenshot https://example.test/a.png", calls))

    def test_ticket_planned_evidence_skips_extra_tool_decision(self):
        class NoDecisionLLM:
            def run(self, role, prompt, timeout=120):
                raise AssertionError("ticket evidence should not ask the model for another tool")

        with mock.patch.object(
            chat_tools,
            "run_model_tool",
            return_value={"ok": True, "tool": "shell", "command": "gitea", "output": "total_open_issues=175"},
        ):
            evidence = harness.collect_tool_evidence(
                NoDecisionLLM(),
                "base",
                "how many tickets are open in gitea?",
                force=True,
            )

        self.assertIn("total_open_issues=175", evidence)

    def test_show_image_accepts_remote_image_url(self):
        class FakeResponse:
            headers = {"Content-Type": "image/png"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, _size):
                return b"\x89PNG\r\n\x1a\n"

        with tempfile.TemporaryDirectory() as td, mock.patch.object(chat_tools, "UPLOADS", Path(td)):
            with mock.patch("jarvis.chat_tools.urllib.request.urlopen", return_value=FakeResponse()):
                result = chat_tools.show_image("https://example.test/image.png")

        self.assertTrue(result["ok"])
        self.assertEqual(result["tool"], "show_image")
        self.assertTrue(result["file"].endswith(".png"))

    def test_execution_profile_heavy_for_owner_correction(self):
        profile = harness.select_execution_profile("your answer doesnt look correct or complete", "")

        self.assertEqual(profile["mode"], "heavy")
        self.assertIn("owner correction requires rechecking", profile["reason"])

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

    def test_force_final_answer_uses_fallback_role(self):
        llm = RoleFallbackLLM({"orchestrator": "", "summarizer": "Final answer from evidence."})

        answer = harness.force_final_answer(llm, "why did tickets jump?", "ticket evidence")

        self.assertEqual(answer, "Final answer from evidence.")
        self.assertEqual(llm.calls[:2], ["orchestrator", "summarizer"])
        self.assertIn("ticket evidence", llm.prompt)

    def test_detects_raw_tool_output_for_answer_question(self):
        raw = "$ cd /repo/audit && TOKEN=...\npage 1: 50\npage 2: 50"

        self.assertTrue(harness.looks_like_raw_tool_output(raw, "why did the ticket count jump?"))
        self.assertFalse(harness.looks_like_raw_tool_output(raw, "show me the raw output"))

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
