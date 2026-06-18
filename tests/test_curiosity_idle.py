import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jarvis import kernel, perceive


class CuriosityIdleTests(unittest.TestCase):
    def _root(self, open_questions=0, explored_at=None):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        qdir = root / "workspace" / "knowledge"
        qdir.mkdir(parents=True)
        questions = [{"q": "gap", "answered": False} for _ in range(open_questions)]
        (qdir / "questions.json").write_text(json.dumps(questions))
        if explored_at is not None:
            state = root / "state"
            state.mkdir()
            (state / "explore.json").write_text(json.dumps({"ts": explored_at}))
        return tmp, root

    def _patch_perceive_sources(self, root, recurring):
        return mock.patch.multiple(
            perceive,
            ROOT=root,
            ledger_signals=mock.Mock(return_value={"recurring": recurring, "recent": []}),
            worksource=mock.Mock(return_value=[]),
        )

    def test_perceive_has_no_curiosity_when_questions_clear_and_discovery_fresh(self):
        tmp, root = self._root(open_questions=0, explored_at=time.time())
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(perceive, "ROOT", root), \
             mock.patch.object(perceive, "ledger_signals", return_value={"recurring": [], "recent": []}):
            world = perceive.perceive({"explore": {"interval_seconds": 21600}})

        self.assertIsNone(world["stalest_area"])

    def test_perceive_curiosity_when_questions_open(self):
        tmp, root = self._root(open_questions=1, explored_at=time.time())
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(perceive, "ROOT", root), \
             mock.patch.object(perceive, "ledger_signals", return_value={"recurring": [], "recent": []}):
            world = perceive.perceive({"explore": {"interval_seconds": 21600}})

        self.assertEqual(world["stalest_area"], "knowledge questions")

    def test_perceive_curiosity_when_discovery_stale(self):
        tmp, root = self._root(open_questions=0, explored_at=time.time() - 9999)
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(perceive, "ROOT", root), \
             mock.patch.object(perceive, "ledger_signals", return_value={"recurring": [], "recent": []}):
            world = perceive.perceive({"explore": {"interval_seconds": 60}})

        self.assertEqual(world["stalest_area"], "environment")

    def test_perceive_surfaces_actionable_self_maintenance(self):
        tmp, root = self._root(open_questions=0, explored_at=time.time())
        self.addCleanup(tmp.cleanup)
        recurring = [{"sig": "deepfix-gap", "label": "tooling-gap", "recurrence": 3}]
        with self._patch_perceive_sources(root, recurring):
            world = perceive.perceive({"explore": {"interval_seconds": 21600}})

        self.assertEqual(world["self_maintenance"], "deepfix-gap")

    def test_perceive_suppresses_self_maintenance_with_unanswered_question(self):
        tmp, root = self._root(open_questions=0, explored_at=time.time())
        self.addCleanup(tmp.cleanup)
        state = root / "state"
        state.mkdir(exist_ok=True)
        (state / "messages.jsonl").write_text(json.dumps({
            "from": "jarvis",
            "kind": "question",
            "text": "What is deepfix-gap?",
            "ref": "improve my own tooling/process: deepfix-gap",
            "answered": False,
        }) + "\n")
        recurring = [{"sig": "deepfix-gap", "label": "tooling-gap", "recurrence": 3}]
        with self._patch_perceive_sources(root, recurring):
            world = perceive.perceive({"explore": {"interval_seconds": 21600}})

        self.assertIsNone(world["self_maintenance"])

    def test_perceive_suppresses_recent_self_maintenance_run(self):
        tmp, root = self._root(open_questions=0, explored_at=time.time())
        self.addCleanup(tmp.cleanup)
        state = root / "state"
        state.mkdir(exist_ok=True)
        (state / "runs.jsonl").write_text(json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "mode": "tick/p4_self_maintenance",
            "target": "improve my own tooling/process: deepfix-gap",
        }) + "\n")
        recurring = [{"sig": "deepfix-gap", "label": "tooling-gap", "recurrence": 3}]
        with self._patch_perceive_sources(root, recurring):
            world = perceive.perceive({"self_maintenance": {"cooldown_seconds": 21600}})

        self.assertIsNone(world["self_maintenance"])

    def test_kernel_idles_when_no_rungs_have_work(self):
        rung, action = kernel.decide(
            {"priorities": ["p0_active_incident", "p5_curiosity"]},
            {
                "active_incident": None,
                "unfinished_wip": None,
                "self_caused_regression": None,
                "needs_human_backlog": [],
                "self_maintenance": None,
                "stalest_area": None,
            },
        )

        self.assertEqual(rung, "idle")
        self.assertEqual(action, "idle: no eligible work")

    def test_p5_curiosity_act_uses_bounded_explore(self):
        decision = {
            "rung": "p5_curiosity",
            "action": "explore stalest area: environment",
            "cognitive_profile": "heavy",
            "cognitive_reason": "autonomous tick",
        }

        def fake_explore(cfg, incoming):
            incoming["worker"] = "curiosity: bounded explore"

        with mock.patch.object(kernel, "_explore", side_effect=fake_explore) as explore, \
             mock.patch("jarvis.adapters.llm.build_llm", side_effect=AssertionError("heavy LLM should not run")), \
             mock.patch("jarvis.harness.run_heavy_task", side_effect=AssertionError("heavy harness should not run")):
            kernel.act({"identity": {"mode": "live"}}, decision, {})

        explore.assert_called_once()
        self.assertEqual(decision["worker"], "curiosity: bounded explore")

    def test_think_skips_p5_curiosity(self):
        decision = {
            "rung": "p5_curiosity",
            "action": "explore stalest area: environment",
        }

        with mock.patch("jarvis.adapters.llm.build_llm", side_effect=AssertionError("LLM should not run")):
            kernel.think({"llm": {"think_on_tick": True}}, decision, {})

        self.assertIn("bounded discovery", decision["thought"])

    def test_p4_self_maintenance_act_uses_deep_fix(self):
        decision = {
            "rung": "p4_self_maintenance",
            "action": "improve my own tooling/process: deepfix-gap",
            "mode": "autonomous",
        }
        world = {"self_maintenance": "deepfix-gap", "needs_human_backlog": []}

        with mock.patch("jarvis.workers.runner.run_worker",
                        return_value={"ok": True, "would": True}) as run_worker:
            kernel.act({}, decision, world)

        run_worker.assert_called_once_with({}, "deep_fix", "deepfix-gap", mode="autonomous")
        self.assertEqual(decision["worker"], "deep_fix(deepfix-gap) [would]")

    def test_think_skips_p4_self_maintenance(self):
        decision = {
            "rung": "p4_self_maintenance",
            "action": "improve my own tooling/process: deepfix-gap",
        }

        with mock.patch("jarvis.adapters.llm.build_llm", side_effect=AssertionError("LLM should not run")):
            kernel.think({"llm": {"think_on_tick": True}}, decision, {})

        self.assertIn("Self-maintenance", decision["thought"])

    def test_think_skips_idle(self):
        decision = {
            "rung": "idle",
            "action": "idle: no eligible work",
        }

        with mock.patch("jarvis.adapters.llm.build_llm", side_effect=AssertionError("LLM should not run")):
            kernel.think({"llm": {"think_on_tick": True}}, decision, {})

        self.assertIn("Idle", decision["thought"])


if __name__ == "__main__":
    unittest.main()
