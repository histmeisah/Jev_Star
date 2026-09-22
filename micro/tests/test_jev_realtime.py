import unittest

from s2clientprotocol import common_pb2 as common
from s2clientprotocol import raw_pb2 as raw
from s2clientprotocol import sc2api_pb2 as sc

from jev_micro.environment import JevMicroEnv
from jev_micro.realtime import executable_choices, request_snapshot, snapshot_commands
from test_jev_micro import fixture


class RealtimeContractTests(unittest.TestCase):
    def setup_snapshot(self):
        env, data = fixture()
        env.micro_data = data
        env.n_agents = 2
        env.check_bounds = lambda x, y: 0 <= x < 32 and 0 <= y < 32

        def action(index, choice):
            unit = env.agents[index]
            command = raw.ActionRawUnitCommand(unit_tags=[unit.tag])
            if choice == 4:
                command.ability_id = 16
                command.target_world_space_pos.CopyFrom(common.Point2D(x=unit.pos.x + 2, y=unit.pos.y))
            elif choice >= 6:
                command.ability_id = 23
                command.target_unit_tag = env.enemies[choice - 6].tag
            else:
                command.ability_id = 4
            return sc.Action(action_raw=raw.ActionRaw(unit_command=command))
        env.get_agent_action = action
        payload = request_snapshot(env, "jev-1.13.0", 1, 480, {}, {}, {})
        return env, payload, snapshot_commands(env, payload)

    def test_realtime_request_does_not_claim_paused_fixed_steps(self):
        env, payload, frozen = self.setup_snapshot()
        self.assertTrue(payload["state"]["realtime"])
        self.assertEqual(payload["state"]["game_loops_remaining"], 400)
        self.assertNotIn("step_mul", payload["state"])
        self.assertNotIn("steps_remaining", payload["state"])
        self.assertIn("battle continues during inference", payload["questions"]["u0"]["instructions"])

    def test_movement_executes_original_endpoint_after_unit_moves(self):
        env, payload, frozen = self.setup_snapshot()
        env.agents[0].pos.x = 12
        commands, applied, dropped = executable_choices(env, payload, {"u0": {"choice": "4"}}, frozen)
        self.assertEqual(commands[0].action_raw.unit_command.target_world_space_pos.x, 11)
        self.assertEqual(applied, {"u0": "4"})
        self.assertEqual(dropped, [])

    def test_dead_actor_is_dropped_without_replacement(self):
        env, payload, frozen = self.setup_snapshot()
        env.agents[0].health = 0
        commands, applied, dropped = executable_choices(env, payload, {"u0": {"choice": "6"}}, frozen)
        self.assertEqual((commands, applied), ([], {}))
        self.assertEqual(dropped[0]["reason"], "actor_dead_or_replaced")

    def test_attack_target_leaving_vision_is_dropped(self):
        env, payload, frozen = self.setup_snapshot()
        env._obs.observation.raw_data.units[1].display_type = 2
        commands, applied, dropped = executable_choices(env, payload, {"u0": {"choice": "6"}}, frozen)
        self.assertEqual((commands, applied), ([], {}))
        self.assertEqual(dropped[0]["reason"], "target_no_longer_visible")

    def test_fixed_step_api_is_rejected_in_realtime(self):
        env = JevMicroEnv.__new__(JevMicroEnv)
        env.realtime = True
        with self.assertRaisesRegex(RuntimeError, "independent observation/action loop"):
            env.step([4, 4, 4])


if __name__ == "__main__":
    unittest.main()
