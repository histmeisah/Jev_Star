import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jev_micro.planner import HIERARCHY_SCHEMA_VERSION, MapPlanner, PlannerError, attach_plan, planning_input, validate_plan
from jev_micro.policy import batch_requests, build_request
from test_jev_micro import fixture, unit


def sample_plan():
    return {"map": "3m", "objective": "Eliminate the enemy team.",
            "opening": "Advance into contact as a group.",
            "target_selection": "Coordinate on the weakest reachable target.",
            "movement_and_cooldowns": "Fire when ready; avoid interrupting an available shot.",
            "priorities": ["Keep damage concentrated."],
            "contingencies": ["Re-evaluate using currently visible enemies."],
            "assumptions": [],
            "unit_roles": [{"unit_type": "Marine", "guidance": "Contribute sustained damage."}]}


class PlanContractTests(unittest.TestCase):
    def test_planner_cannot_read_hidden_enemy_ground_truth(self):
        env, data = fixture()
        payload = build_request(env, data, "jev-1.13.0", 1)
        initial = planning_input(payload, env)
        self.assertNotIn("777", json.dumps(initial))
        env.enemies[1].health = 123456
        env.enemies[1].pos.x = 10000
        self.assertEqual(initial, planning_input(build_request(env, data, "jev-1.13.0", 1), env))
        self.assertFalse(initial["control"]["realtime"])

    def test_plan_guides_without_pruning_or_selecting_actions(self):
        env, data = fixture()
        payload = build_request(env, data, "jev-1.13.0", 1)
        original = copy.deepcopy(payload)
        active = {"model": "gpt-6-astra", "plan_sha256": "test", "plan": sample_plan()}
        attached = attach_plan(payload, active)
        self.assertEqual(original, payload)
        self.assertEqual(attached["state"]["schema_version"], HIERARCHY_SCHEMA_VERSION)
        for actor, question in payload["questions"].items():
            self.assertEqual(attached["questions"][actor]["criteria"], question["criteria"])
            self.assertIn("state.strategic_plan.plan", attached["questions"][actor]["instructions"])
        self.assertEqual(attached["state"]["strategic_plan"], active)
        attached["state"]["strategic_plan"]["plan"]["objective"] = "changed"
        self.assertEqual(active["plan"], sample_plan())

    def test_full_plan_survives_large_team_batching(self):
        env, data = fixture()
        env.agents = {i: unit(1000 + i, 1, 9) for i in range(58)}
        env.enemies = {i: unit(2000 + i, 2, 14) for i in range(50)}
        env._obs.observation.raw_data.ClearField("units")
        env._obs.observation.raw_data.units.extend([*env.agents.values(), *env.enemies.values()])
        env.get_avail_agent_actions = lambda i: [0] + [1] * 55
        payload = build_request(env, data, "jev-1.13.0", 1)
        active = {"model": "gpt-6-astra", "plan_sha256": "test", "plan": sample_plan()}
        attached = attach_plan(payload, active)
        batches = batch_requests(attached)
        self.assertGreater(len(batches), 1)
        actors = []
        for batch in batches:
            self.assertEqual(batch["state"]["strategic_plan"], active)
            actors.extend(batch["questions"])
        self.assertEqual(set(actors), set(payload["questions"]))
        self.assertEqual(len(actors), len(set(actors)))

    def test_bad_plans_rejected_without_silent_truncation(self):
        wrong_map = sample_plan()
        wrong_map["map"] = "2s3z"
        with self.assertRaisesRegex(PlannerError, "wrong_map"):
            validate_plan(wrong_map, "3m")
        too_long = sample_plan()
        too_long["opening"] = "x" * 1001
        with self.assertRaisesRegex(PlannerError, "invalid_text"):
            validate_plan(too_long, "3m")
        extra = sample_plan()
        extra["action_override"] = [6, 6, 6]
        with self.assertRaisesRegex(PlannerError, "invalid_fields"):
            validate_plan(extra, "3m")


class MapPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_request_reused_across_three_episodes(self):
        env, data = fixture()
        events, prompts = [], []
        with tempfile.TemporaryDirectory() as folder, patch("jev_micro.planner.find_codex", return_value=Path("codex.exe")):
            planner = MapPlanner(Path(folder) / "planner", lambda event, **kw: events.append((event, kw)))
            def respond(prompt):
                prompts.append(prompt)
                return {"stdout": json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}),
                        "plan_text": json.dumps(sample_plan()), "error": None}
            planner._request = respond
            try:
                plans = []
                for episode in (1, 2, 3):
                    payload = build_request(env, data, "jev-1.13.0", episode)
                    plans.append(await planner.get_plan(payload, env))
                self.assertEqual(len(prompts), 1)
                self.assertEqual(plans[0], plans[1])
                self.assertEqual(plans[1], plans[2])
                self.assertEqual(planner.stats["requests"], 1)
                self.assertEqual([event for event, _ in events], ["planner_request", "planner_response", "plan_accepted"])
                self.assertEqual(events[0][1]["prompt"], prompts[0])
                plans[0]["plan"]["objective"] = "mutated"
                self.assertEqual(planner.active["plan"], sample_plan())
            finally:
                await planner.close()

    async def test_invalid_response_keeps_usage_and_never_activates_plan(self):
        env, data = fixture()
        with tempfile.TemporaryDirectory() as folder, patch("jev_micro.planner.find_codex", return_value=Path("codex.exe")):
            planner = MapPlanner(Path(folder) / "planner", lambda *a, **kw: None)
            planner._request = lambda prompt: {
                "stdout": json.dumps({"type": "turn.completed", "usage": {"input_tokens": 321}}),
                "plan_text": "{}", "error": None}
            try:
                with self.assertRaises(PlannerError):
                    await planner.get_plan(build_request(env, data, "jev-1.13.0", 1), env)
                self.assertIsNone(planner.active)
                self.assertEqual(planner.stats["errors"], 1)
                self.assertEqual(planner.stats["usage"]["input_tokens"], 321)
            finally:
                await planner.close()


if __name__ == "__main__":
    unittest.main()
