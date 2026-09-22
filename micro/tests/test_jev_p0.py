import copy
import hashlib
import importlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from s2clientprotocol import common_pb2, data_pb2, error_pb2, sc2api_pb2 as sc

from jev_micro.catalog import SUPPORTED_MAPS, map_source
from jev_micro.environment import StarCraft2Env
from jev_micro.feedback import action_feedback
from jev_micro.planner import attach_plan, planning_input, validate_plan
from jev_micro.policy import TEAM_CORE_FIELDS, ORDER_FIELDS, batch_requests, build_request, team_core
from jev_micro.scenario import build_scenario_brief, map_facts, terrain_brief, opponent_brief, unpack_world_grid
from jev_micro.environment import opponent_for
import test_jev_micro as fixtures
from test_jev_micro import fixture, unit
from test_jev_planner import sample_plan


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


class RangeSemanticsTests(unittest.TestCase):
    def test_native_queen_range_does_not_expand_smac_action_space(self):
        env = fixtures.ExtendedMapTests().environment()
        queen, target = env.agents[0], env.enemies[0]
        queen.unit_type = 126
        env.micro_data.units[126] = "Queen"
        env.micro_data.unit_stats[126] = data_pb2.UnitTypeData(weapons=[data_pb2.Weapon(type=2, range=7)])
        target.unit_type, target.is_flying, target.pos.x = 57, True, 16.5
        env._obs.observation.raw_data.units[-1].CopyFrom(target)
        before = env.get_avail_agent_actions(0)
        payload = build_request(env, env.micro_data, "jev-1.13.0", 1)
        r = payload["state"]["relations"]["u0"]["e0"]
        self.assertTrue(r["in_weapon_range"])
        self.assertFalse(r["smac_center_distance_allowed"])
        self.assertFalse(r["smac_attack_available"])
        self.assertNotIn("6", payload["questions"]["u0"]["criteria"])
        self.assertEqual(before, env.get_avail_agent_actions(0))

    def test_missing_effect_weapon_is_unknown_not_out_of_range(self):
        env = fixtures.ExtendedMapTests().environment()
        env.agents[0].unit_type = 80
        env.micro_data.units[80] = "VoidRay"
        env.micro_data.unit_stats[80] = data_pb2.UnitTypeData()
        payload = build_request(env, env.micro_data, "jev-1.13.0", 1)
        r = payload["state"]["relations"]["u0"]["e0"]
        self.assertIsNone(r["in_weapon_range"])
        self.assertEqual(r["weapon_range_status"], "unknown")
        self.assertIsNone(r["approach_needed"])
        self.assertTrue(r["smac_attack_available"])

    def test_incompatible_weapon_is_distinct_from_missing_data(self):
        env = fixtures.ExtendedMapTests().environment()
        env.enemies[0].is_flying = True
        env._obs.observation.raw_data.units[-1].CopyFrom(env.enemies[0])
        payload = build_request(env, env.micro_data, "jev-1.13.0", 1)
        r = payload["state"]["relations"]["u1"]["e0"]
        self.assertEqual(r["weapon_range_status"], "no_compatible_weapon")
        self.assertFalse(r["smac_attack_available"])


class FeedbackTests(unittest.TestCase):
    def test_results_follow_submitted_commands_not_actor_indices(self):
        env = fixtures.ExtendedMapTests().environment()
        env.agents[1].health = 0
        env.agents[2].orders.add(ability_id=386, target_unit_tag=env.agents[0].tag)
        def step(_env, choices):
            for actor, action in enumerate(choices):
                _env.get_agent_action(actor, action)
            _env.last_action_results = [sc.ResponseAction(result=[error_pb2.Success])]
            return 0, False, {}
        with patch.object(StarCraft2Env, "step", step):
            env.step([6, 0, 6])
            feedback = action_feedback(env, 0)
            self.assertEqual(feedback["response_mapping"], "exact")
            self.assertEqual(feedback["actors"]["u0"]["submission"], "accepted")
            self.assertEqual(feedback["actors"]["u1"]["submission"], "dead_unit_noop")
            self.assertEqual(feedback["actors"]["u2"]["submission"], "existing_heal_order_retained")
            self.assertEqual(feedback["actors"]["u0"]["command"]["target_unit"], "e0")
            self.assertIsNone(feedback["actors"]["u2"]["response"])
            env.step([6, 0, 6])
            self.assertEqual(len(env.last_submissions), 3)
        self.assertFalse(env._collecting_actions)

    def test_rejections_and_delayed_errors_reach_next_request_with_raw_ids(self):
        env = fixtures.ExtendedMapTests().environment()
        env._collecting_actions, env.last_submissions = True, []
        env.get_agent_action(0, 6)
        env.get_agent_action(2, 6)
        env.last_action_results = [sc.ResponseAction(result=[error_pb2.Success, error_pb2.AlreadyTargeted])]
        env._obs.action_errors.add(unit_tag=12, ability_id=386, result=error_pb2.NotEnoughEnergy)
        previous = action_feedback(env, 8)
        payload = build_request(env, env.micro_data, "jev-1.13.0", 1, previous_result=previous)
        actor = payload["state"]["last_action_result"]["actors"]["u2"]
        self.assertEqual(actor["response"]["name"], "AlreadyTargeted")
        self.assertEqual(actor["command"]["kind"], "heal")
        self.assertEqual(actor["command"]["target_unit"], "u0")
        self.assertEqual(actor["command"]["target_tag"], "10")
        self.assertEqual(actor["observation_errors"][0]["name"], "NotEnoughEnergy")
        previous["actors"]["u2"]["submission"] = "mutated"
        self.assertEqual(actor["submission"], "rejected")
        self.assertIsNone(build_request(env, env.micro_data, "jev-1.13.0", 2)["state"]["last_action_result"])
        env.last_action_results = [sc.ResponseAction(result=[error_pb2.Success])]
        mismatch = action_feedback(env, 8)
        self.assertEqual(mismatch["response_mapping"], "count_mismatch")
        self.assertIsNone(mismatch["actors"]["u0"]["response"])


class ScenarioTests(unittest.TestCase):
    def test_native_rectangular_pixels_are_world_xy_without_y_flip(self):
        layer = common_pb2.ImageData(bits_per_pixel=8, size=common_pb2.Size2DI(x=3, y=2),
                                     data=bytes([1, 2, 3, 11, 12, 13]))
        decoded = unpack_world_grid(layer)
        self.assertEqual(decoded.shape, (3, 2))
        self.assertEqual(decoded[0, 0], 1)
        self.assertEqual(decoded[0, 1], 11)
        self.assertEqual(decoded[2, 0], 3)
        layer.bits_per_pixel = 1
        layer.data = bytes([0b10011000])
        self.assertEqual(unpack_world_grid(layer).tolist(), [[1, 1], [0, 1], [0, 0]])

    def test_described_base_destinations_match_real_script_commands_on_all_maps(self):
        for name in SUPPORTED_MAPS:
            with self.subTest(map=name):
                env = SimpleNamespace(map_name=name)
                env.dts_script = opponent_for(env, "base")
                brief = opponent_brief(env, "base")
                module = importlib.import_module(type(env.dts_script).__module__)
                with patch.object(module, "attack", side_effect=lambda u, dest, visibility: list(dest)):
                    own, enemy = {0: unit(10, 2, 20)}, {0: unit(20, 1, 10)}
                    self.assertEqual(env.dts_script.script(own, enemy, [], {}, 4), [])
                    actual = env.dts_script.script(own, enemy, [], {}, 5)
                expected = brief["attack_move_destination"]
                self.assertEqual(actual, [expected] if expected is not None else [])

    def test_all_35_actual_assets_match_registry_and_have_hashed_sources(self):
        for name in SUPPORTED_MAPS:
            with self.subTest(map=name):
                facts = map_facts(name)
                self.assertEqual(facts["map_sha256"], hashlib.sha256(map_source(name).read_bytes()).hexdigest())
                self.assertTrue(all(s["spawn_anchor"] is not None for s in facts["initial_spawns"]))
        self.assertEqual(map_facts("corridor")["rosters"], {"allies": {"Zealot": 6}, "enemies": {"Zergling": 24}})

    def test_exact_wall_opening_and_nonzero_playable_origin(self):
        env = SimpleNamespace(map_x=8, map_y=8, playable_area=[[1, 1], [7, 7]],
                              pathing_grid=np.zeros((8, 8), bool), terrain_height=np.zeros((8, 8)))
        env.pathing_grid[1:7, 1:7] = True
        env.pathing_grid[3, 1:7] = False
        env.pathing_grid[3, 3] = True
        env.terrain_height[4:7, 1:7] = 0.5
        result = terrain_brief(env)
        spans = result["walkable_spans"]
        self.assertIn({"y": [3, 4], "x_intervals": [[1, 7]]}, spans)
        self.assertIn({"y": [1, 3], "x_intervals": [[1, 3], [4, 7]]}, spans)
        self.assertEqual(len(result["ground_components_4_connected"]), 1)
        self.assertEqual(result["ground_components_4_connected"][0]["cells"], 31)
        self.assertEqual(result["walkable_height_histogram_normalized"][0.5], 18)

    def test_planner_gets_static_brief_without_hidden_runtime_state(self):
        env, data = fixture()
        env.agents = {i: unit(100 + i, 1, 9) for i in range(3)}
        env.micro_data = data
        env.dts_script = opponent_for(env, "base")
        env.scenario_brief = build_scenario_brief(env, "base")
        first = planning_input(build_request(env, data, "jev-1.13.0", 1), env)
        self.assertEqual(first["scenario_brief"]["public_map_facts"]["rosters"]["enemies"], {"Marine": 3})
        self.assertEqual(first["scenario_brief"]["opponent"]["attack_move_destination"], [9, 16])
        env.enemies[1].health, env.enemies[1].pos.x = 123456, 87654
        env.scenario_brief = build_scenario_brief(env, "base")
        self.assertEqual(first, planning_input(build_request(env, data, "jev-1.13.0", 1), env))


class CompleteBatchTests(unittest.TestCase):
    def test_readiness_is_not_inferred_from_rounded_cooldown(self):
        env, data = fixture()
        env.agents[0].weapon_cooldown = 0.004
        payload = build_request(env, data, "jev-1.13.0", 1)
        core = team_core(payload["state"])
        row = dict(zip(core["ally_columns"], core["allies"]["u0"]))
        self.assertEqual(row["weapon_cooldown_loops"], 0)
        self.assertEqual(row["weapon_ready"], 0)

    def test_full_team_core_and_feedback_with_near_limit_plan(self):
        env, data = fixture()
        env.agents = {i: unit(1000000000000000000 + i, 1, 9 + i / 100) for i in range(58)}
        env.enemies = {i: unit(2000000000000000000 + i, 2, 14) for i in range(30)}
        for i, u in env.agents.items():
            u.weapon_cooldown = i % 4 * 3.37
            u.health, u.energy = 20 + i % 20, 70
            u.orders.add(ability_id=23, target_unit_tag=env.enemies[i % 30].tag)
        env._obs.observation.raw_data.ClearField("units")
        env._obs.observation.raw_data.units.extend([*env.agents.values(), *env.enemies.values()])
        env.get_avail_agent_actions = lambda i: [0] + [1] * 35
        payload = build_request(env, data, "jev-1.13.0", 1, previous_health={u.tag: 45 for u in env.agents.values()})
        feedback = {"from_game_loop": 72, "to_game_loop": 80, "response_mapping": "exact", "actors": {},
                    "raw_response_results": [1] * 58, "unmatched_observation_errors": []}
        for actor, u in payload["state"]["allies"].items():
            feedback["actors"][actor] = {"requested_action_id": "6", "submission": "accepted",
                "command_index": int(actor[1:]), "command": u["orders"][0], "response": {"code": 1, "name": "Success"},
                "observation_errors": []}
        payload["state"]["last_action_result"] = feedback
        plan = sample_plan()
        for field in ("objective", "opening", "target_selection", "movement_and_cooldowns"):
            plan[field] = "x" * 1000
        plan["priorities"] = ["p" * 500] * 2
        plan["contingencies"] = ["c" * 400]
        validate_plan(plan, env.map_name)
        attached = attach_plan(payload, {"model": "gpt-6-astra", "plan_sha256": "test", "plan": plan})
        before = copy.deepcopy(attached)
        batches = batch_requests(attached)
        self.assertGreater(len(batches), 1)
        actors = []
        first_core = batches[0]["state"]["team_core"]
        for batch in batches:
            state = batch["state"]
            self.assertEqual(state["team_core"], first_core)
            self.assertEqual(state["strategic_plan"], attached["state"]["strategic_plan"])
            self.assertLessEqual(encoded_size(state) + max(map(encoded_size, batch["questions"].values())), 30000)
            self.assertLessEqual(encoded_size(batch), 56000)
            for actor in batch["questions"]:
                self.assertEqual(batch["questions"][actor]["criteria"], attached["questions"][actor]["criteria"])
                self.assertEqual(state["last_action_result"]["actors"][actor], feedback["actors"][actor])
                for enemy, values in state["relations"][actor].items():
                    restored = dict(zip(state["relation_columns"], values))
                    expected = attached["state"]["relations"][actor][enemy]
                    self.assertEqual(restored, {k: expected[k] for k in state["relation_columns"]})
                for action, values in state["movement_candidates"][actor].items():
                    restored = dict(zip(state["movement_columns"], values))
                    restored["distances_to_visible_enemies"] = dict(zip(
                        state["movement_enemy_columns"], restored["distances_to_visible_enemies"]))
                    self.assertEqual(restored, attached["state"]["movement_candidates"][actor][action])
                actors.append(actor)
        self.assertEqual(sorted(actors), sorted(payload["questions"]))
        for actor, u in payload["state"]["allies"].items():
            row = dict(zip(first_core["ally_columns"], first_core["allies"][actor]))
            for field in TEAM_CORE_FIELDS[:-1]:
                self.assertEqual(row[field], u[field])
            self.assertEqual([dict(zip(ORDER_FIELDS, first_core["order_table"][i])) for i in row["orders"]], u["orders"])
        self.assertEqual(attached, before)


if __name__ == "__main__":
    unittest.main()
