import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import chat_tools, harness, messaging, run_trace
from jarvis.dashboard import server


class ChatObservabilityTests(unittest.TestCase):
    def test_stream_end_persists_duration_and_trace_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(messaging, "STATE", root), \
                 mock.patch.object(messaging, "MSGS", root / "messages.jsonl"), \
                 mock.patch.object(messaging, "CONV_META", root / "conv_meta.json"), \
                 mock.patch.object(messaging, "_LOCKFILE", root / ".messages.lock"):
                mid = messaging.stream_start("chat-test")
                messaging.stream_end(
                    mid,
                    "done",
                    duration_ms=1234,
                    trace_id="chat-abc",
                    trace_path="state/chat_traces/chat-abc.jsonl",
                )

                rows = messaging.messages("chat-test")

        self.assertEqual(rows[0]["duration_ms"], 1234)
        self.assertEqual(rows[0]["trace_id"], "chat-abc")
        self.assertEqual(rows[0]["trace_path"], "state/chat_traces/chat-abc.jsonl")
        self.assertFalse(rows[0]["streaming"])

    def test_stream_update_persists_live_status_and_trace_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(messaging, "STATE", root), \
                 mock.patch.object(messaging, "MSGS", root / "messages.jsonl"), \
                 mock.patch.object(messaging, "CONV_META", root / "conv_meta.json"), \
                 mock.patch.object(messaging, "_LOCKFILE", root / ".messages.lock"):
                mid = messaging.stream_start("chat-test")
                messaging.stream_update(
                    mid,
                    "",
                    status="checking ticket evidence",
                    trace_id="chat-live",
                    trace_path="state/chat_traces/chat-live.jsonl",
                )

                rows = messaging.messages("chat-test")

        self.assertEqual(rows[0]["status"], "checking ticket evidence")
        self.assertEqual(rows[0]["trace_id"], "chat-live")
        self.assertEqual(rows[0]["trace_path"], "state/chat_traces/chat-live.jsonl")
        self.assertTrue(rows[0]["streaming"])

    def test_trace_redacts_secret_like_values(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(run_trace, "TRACE_DIR", Path(td)):
                trace = run_trace.ChatRunTrace("general", "owner")
                trace.event("tool_call_end", args={"cmd": "curl -H 'Authorization: Bearer abc123' TOKEN=xyz"})

                raw = trace.path.read_text()

        self.assertIn("Authorization: Bearer [redacted]", raw)
        self.assertIn("TOKEN=[redacted]", raw)
        self.assertNotIn("abc123", raw)
        self.assertNotIn("xyz", raw)

    def test_chat_trace_endpoint_reads_message_trace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trace_dir = root / "state" / "chat_traces"
            trace_dir.mkdir(parents=True)
            trace_path = trace_dir / "chat-abc.jsonl"
            trace_path.write_text('{"event":"chat_start"}\n{"event":"chat_finish"}\n')
            row = {
                "id": "msg1",
                "ts": "2026-06-18T00:00:00",
                "from": "jarvis",
                "conv": "general",
                "kind": "message",
                "text": "done",
                "trace_id": "chat-abc",
                "trace_path": "state/chat_traces/chat-abc.jsonl",
                "duration_ms": 55,
            }
            with mock.patch.object(server, "ROOT", root), \
                 mock.patch.object(messaging, "STATE", root / "state"), \
                 mock.patch.object(messaging, "MSGS", root / "state" / "messages.jsonl"), \
                 mock.patch.object(messaging, "CONV_META", root / "state" / "conv_meta.json"), \
                 mock.patch.object(messaging, "_LOCKFILE", root / "state" / ".messages.lock"):
                messaging._append(row)
                data = server._chat_trace("msg1")

        self.assertTrue(data["ok"])
        self.assertEqual(data["run_id"], "chat-abc")
        self.assertEqual(data["duration_ms"], 55)
        self.assertEqual([e["event"] for e in data["events"]], ["chat_start", "chat_finish"])

    def test_tool_evidence_logs_decision_start_and_stops_after_repeated_failures(self):
        class FakeLLM:
            calls = 0

            def run(self, role, prompt, timeout=120):
                self.calls += 1
                return '{"tool":"shell","args":{"cmd":"bad-command-%d"}}' % self.calls

        class FakeTrace:
            def __init__(self):
                self.events = []

            def event(self, name, **data):
                self.events.append((name, data))

        statuses = []
        trace = FakeTrace()

        def fake_run_model_tool(call, owner_text):
            return {"ok": False, "tool": call["tool"], "command": call["args"]["cmd"], "error": "failed"}

        with mock.patch.object(chat_tools, "run_model_tool", side_effect=fake_run_model_tool):
            evidence = harness.collect_tool_evidence(
                FakeLLM(),
                "base",
                "check the pipeline",
                status=statuses.append,
                force=True,
                trace=trace,
            )

        event_names = [name for name, _ in trace.events]
        self.assertEqual(event_names.count("tool_decision_start"), 2)
        self.assertIn("tool_loop_done", event_names)
        self.assertIn("stopping after repeated failed tool checks", statuses)
        self.assertIn("bad-command-1", evidence)
        self.assertIn("bad-command-2", evidence)


if __name__ == "__main__":
    unittest.main()
