"""Random baseline must preserve the decision interface without contacting models."""

import contextlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sc2_rl_agent.starcraftenv_test.agent.jev_agent import DecisionScheduler
from sc2_rl_agent.starcraftenv_test.agent.random_macro import RandomMacroClient


class RandomPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_seeded_sequence_ignores_state_order_and_global_rng(self):
        first, second = RandomMacroClient(123), RandomMacroClient(123)
        choices = {71: "EMPTY ACTION", 64: "MULTI-ATTACK", 0: "TRAIN PROBE"}
        sequence = []
        for index in range(80):
            a = await first.choose(first.payload({"minerals": index}, choices))
            random.random()
            b = await second.choose(second.payload({"minerals": 99999}, dict(reversed(list(choices.items())))))
            self.assertEqual(a, b)
            self.assertEqual(set(a["answer"]["probabilities"].values()), {1 / 3})
            sequence.append(a["answer"]["choice"])
        self.assertEqual(set(sequence), {"0", "64", "71"})

    async def test_changing_mask_never_samples_an_unavailable_action(self):
        client = RandomMacroClient(7)
        for choices in ({71: "EMPTY ACTION"}, {0: "PROBE", 19: "PYLON", 71: "EMPTY ACTION"}):
            for _ in range(40):
                result = await client.choose(client.payload({}, choices))
                self.assertIn(int(result["answer"]["choice"]), choices)
                self.assertEqual(result["usage"], {"input_tokens": 0, "output_tokens": 0})
        await client.close()

    async def test_existing_scheduler_applies_same_cadence(self):
        now = [0.0]
        scheduler = DecisionScheduler(RandomMacroClient(1), lambda *a, **k: None,
                                      interval=1, clock=lambda: now[0])
        self.assertTrue(scheduler.submit(0, {}, {0: "PROBE", 71: "WAIT"}))
        await scheduler.task
        self.assertIsNotNone(scheduler.poll(4))
        self.assertFalse(scheduler.ready)
        now[0] = 1
        self.assertTrue(scheduler.ready)
        self.assertEqual(scheduler.stats["input_tokens"], 0)
        await scheduler.close()

    async def test_accelerated_game_cadence_is_independent_of_wall_clock(self):
        wall, game = [100.0], [10.0]
        scheduler = DecisionScheduler(RandomMacroClient(1), lambda *a, **k: None,
                                      interval=1, clock=lambda: wall[0], cadence_clock=lambda: game[0])
        self.assertTrue(scheduler.submit(224, {}, {0: "PROBE", 71: "WAIT"}))
        await scheduler.task
        wall[0] += .001
        self.assertIsNotNone(scheduler.poll(228))
        self.assertFalse(scheduler.ready)
        game[0] += 1.1
        self.assertTrue(scheduler.ready)
        self.assertLess(wall[0] - 100, .01)
        await scheduler.close()


class RandomRunnerTests(unittest.TestCase):
    def test_random_runner_never_loads_credentials_or_constructs_model_client(self):
        from sc2_rl_agent.starcraftenv_test import run_jev
        from sc2_rl_agent.starcraftenv_test.utils import sc2_runtime
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            with patch("sys.argv", ["run_jev", "--policy", "random", "--no-realtime", "--seed", "42", "--output-dir", str(output)]), \
                 patch.object(run_jev, "load_api_key", side_effect=AssertionError("credential access")), \
                 patch.object(run_jev, "JevClient", side_effect=AssertionError("model client")), \
                 patch("sc2.maps.get", return_value=SimpleNamespace(path=Path(tmp) / "test.SC2Map", data=b"test")), \
                 patch.object(sc2_runtime, "run_windowed_game", side_effect=RuntimeError("test startup failure")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_jev.main(), 1)
            settings = json.loads((output / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["policy_seed"], 42)
            self.assertEqual(settings["model"], RandomMacroClient.model)
            self.assertFalse(settings["model_api_calls"])
            self.assertFalse(settings["realtime"])
            self.assertEqual(settings["decision_timebase"], "game")

    def test_accelerated_random_with_planner_is_rejected(self):
        from sc2_rl_agent.starcraftenv_test import run_jev
        with patch("sys.argv", ["run_jev", "--policy", "random", "--planner", "codex", "--no-realtime"]), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                run_jev.main()
        self.assertEqual(error.exception.code, 2)

    def test_random_with_astra_keeps_planner_without_a_jev_client(self):
        from sc2_rl_agent.starcraftenv_test import run_jev
        from sc2_rl_agent.starcraftenv_test.agent import astra_planner
        from sc2_rl_agent.starcraftenv_test.env.bot.hierarchical_protoss_bot import HierarchicalProtossBot
        from sc2_rl_agent.starcraftenv_test.utils import sc2_runtime

        planner = SimpleNamespace(model="gpt-6-astra", timeout=60, effort="medium", close=AsyncMock())
        seen = []

        def inspect_game(game_map, players, **kwargs):
            bot = players[0].ai
            self.assertIsInstance(bot, HierarchicalProtossBot)
            self.assertIsInstance(bot.scheduler.client, RandomMacroClient)
            self.assertIs(bot.planner.client, planner)
            self.assertEqual(bot.plan_mode, "constrained")
            self.assertTrue(kwargs["realtime"])
            seen.append(bot)
            raise RuntimeError("test stops before launching SC2")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            with patch("sys.argv", ["run_jev", "--policy", "random", "--planner", "codex",
                                   "--plan-mode", "constrained", "--seed", "5", "--output-dir", str(output)]), \
                 patch.object(run_jev, "load_api_key", side_effect=AssertionError("JEV credentials requested")), \
                 patch.object(run_jev, "JevClient", side_effect=AssertionError("JEV client constructed")), \
                 patch.object(astra_planner, "CodexPlannerClient", return_value=planner), \
                 patch("sc2.maps.get", return_value=SimpleNamespace(path=Path(tmp) / "test.SC2Map", data=b"test")), \
                 patch.object(sc2_runtime, "run_windowed_game", side_effect=inspect_game), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_jev.main(), 1)
            self.assertEqual(len(seen), 1)
            settings = json.loads((output / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["configuration"], "astra_constrained_random")
            self.assertEqual(settings["policy_seed"], 5)
            self.assertFalse(settings["decision_model_api_calls"])
            self.assertTrue(settings["planner_model_api_calls"])
            self.assertTrue(settings["model_api_calls"])
            self.assertEqual(settings["decision_timebase"], "wall")


class AblationSummaryTests(unittest.TestCase):
    def test_ties_count_in_denominator_but_invalid_attempts_do_not(self):
        from sc2_rl_agent.starcraftenv_test.run_random_ablation import summarize
        summary = summarize([
            {"valid": True, "result": "Victory"}, {"valid": True, "result": "Defeat"},
            {"valid": True, "result": "Tie"}, {"valid": False, "result": "Defeat"}], 3)
        self.assertEqual(summary["verified_games"], 3)
        self.assertEqual(summary["invalid_attempts"], 1)
        self.assertEqual(summary["win_rate"], 1 / 3)
        self.assertEqual((summary["wins"], summary["losses"], summary["ties"]), (1, 1, 1))

    def test_zero_wins_still_has_nonzero_uncertainty(self):
        from sc2_rl_agent.starcraftenv_test.run_random_ablation import wilson_interval
        low, high = wilson_interval(0, 10)
        self.assertAlmostEqual(low, 0)
        self.assertAlmostEqual(high, .2775327998628892)
        self.assertIsNone(wilson_interval(0, 0))


if __name__ == "__main__":
    unittest.main()
