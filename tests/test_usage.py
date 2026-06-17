"""Usage aggregation (dashboard._usage) — per-model activity from run records. Run log is faked."""
import time
import unittest

from jarvis import runtime
from jarvis.dashboard import server


class UsageTests(unittest.TestCase):
    def setUp(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._orig = runtime.recent
        self._rows = [
            {"ts": now, "model": "claude:claude-sonnet-4-6", "status": "ok", "duration": 2.0},
            {"ts": now, "model": "claude:claude-sonnet-4-6", "status": "failed", "duration": 4.0},
            {"ts": now, "model": "ollama_cloud:deepseek-v4-pro", "status": "ok", "duration": None},
        ]
        runtime.recent = lambda limit=5000: list(self._rows)

    def tearDown(self):
        runtime.recent = self._orig

    def test_windows_present(self):
        u = server._usage()
        self.assertEqual([w["label"] for w in u["windows"]], ["24h", "7d", "all"])

    def test_per_model_aggregation(self):
        w = server._usage()["windows"][0]            # 24h
        self.assertEqual(w["total"], 3)
        by = {m["model"]: m for m in w["models"]}
        sonnet = by["claude:claude-sonnet-4-6"]
        self.assertEqual(sonnet["calls"], 2)
        self.assertEqual(sonnet["ok"], 1)
        self.assertEqual(sonnet["failed"], 1)
        self.assertEqual(sonnet["avg_s"], 3.0)       # (2+4)/2
        self.assertEqual(sonnet["total_s"], 6.0)

    def test_null_duration_handled(self):
        by = {m["model"]: m for m in server._usage()["windows"][0]["models"]}
        deepseek = by["ollama_cloud:deepseek-v4-pro"]
        self.assertEqual(deepseek["calls"], 1)
        self.assertIsNone(deepseek["avg_s"])         # no numeric durations -> None, not a crash

    def test_sorted_by_calls_desc(self):
        models = server._usage()["windows"][0]["models"]
        self.assertEqual(models[0]["model"], "claude:claude-sonnet-4-6")  # 2 calls > 1

    def test_notes_present(self):
        self.assertTrue(any("no per-call cost" in n for n in server._usage()["notes"]))


if __name__ == "__main__":
    unittest.main()
