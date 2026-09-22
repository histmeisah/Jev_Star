import asyncio
import copy
import json
import unittest
from types import SimpleNamespace

import httpx
from s2clientprotocol import raw_pb2 as raw
from s2clientprotocol import sc2api_pb2 as sc
from s2clientprotocol import data_pb2

from jev_micro.client import JevClient, JevError, validate_response
from jev_micro.policy import build_request, batch_requests, decode_actions, nearest_actions
from jev_micro.environment import JevMicroEnv
from jev_micro.run import battle_state


def unit(tag, owner, x, health=45, visible=True):
    u = raw.Unit(tag=tag, owner=owner, alliance=1 if owner == 1 else 4,
                 unit_type=48, health=health, health_max=45, radius=0.375,
                 display_type=1 if visible else 2)
    u.pos.x, u.pos.y = x, 16
    return u


def fixture():
    own, dead = unit(100, 1, 9), unit(101, 1, 10, health=0)
    visible, hidden = unit(200, 2, 14, health=39), unit(201, 2, 27, health=777, visible=False)
    obs = sc.ResponseObservation()
    obs.observation.game_loop = 80
    obs.observation.raw_data.units.extend([own, visible, hidden])
    env = SimpleNamespace(
        map_name="3m", use_ability=False, agents={0: own, 1: dead},
        enemies={0: visible, 1: hidden}, _obs=obs, _step_mul=8,
        _episode_steps=10, episode_limit=60, _move_amount=2, map_x=32, map_y=32,
        get_avail_agent_actions=lambda i: [0, 1, 1, 1, 1, 1, 1, 1] if i == 0 else [1] + [0]*7)
    data = SimpleNamespace(units={48: "Marine"}, unit_stats={48: SimpleNamespace(weapons=[SimpleNamespace(range=5)])})
    return env, data


class PolicyTests(unittest.TestCase):
    def test_unseen_enemy_is_absent_from_state_and_actions(self):
        env, data = fixture()
        payload = build_request(env, data, "jev-1.13.0", 1)
        self.assertEqual(set(payload["state"]["visible_enemies"]), {"e0"})
        self.assertNotIn("777", json.dumps(payload))
        self.assertNotIn("7", payload["questions"]["u0"]["criteria"])
        # Mutating hidden ground truth cannot affect the model's request.
        env.enemies[1].pos.x = 1
        env.enemies[1].health = 1
        env.enemies[1].unit_type = 999
        self.assertEqual(payload, build_request(env, data, "jev-1.13.0", 1))

    def test_living_units_have_complete_choices_and_dead_units_no_question(self):
        env, data = fixture()
        payload = build_request(env, data, "jev-1.13.0", 1)
        self.assertEqual(set(payload["questions"]), {"u0"})
        self.assertNotIn("0", payload["questions"]["u0"]["criteria"])
        self.assertEqual(decode_actions(payload, {"u0": {"choice": "6"}}, 2), [6, 0])
        self.assertEqual(nearest_actions(payload, 2), [6, 0])
        with self.assertRaises(ValueError):
            decode_actions(payload, {"u0": {"choice": "7"}}, 2)

    def test_snapshot_is_immutable_and_north_uses_world_coordinates(self):
        env, data = fixture()
        payload = build_request(env, data, "jev-1.13.0", 1, {100: 50})
        self.assertEqual(payload["state"]["movement_candidates"]["u0"]["2"]["destination"], [9, 18])
        self.assertEqual(payload["state"]["allies"]["u0"]["health_lost_since_previous_decision"], 5)
        env.agents[0].health = 10
        self.assertEqual(payload["state"]["allies"]["u0"]["health"], 45)

    def test_protoss_shields_and_asymmetric_weapon_ranges(self):
        env, data = fixture()
        env.map_name = "2s3z"
        stalker, zealot = env.agents[0], env.enemies[0]
        stalker.unit_type, stalker.health, stalker.health_max = 74, 60, 80
        stalker.shield, stalker.shield_max = 60, 80
        zealot.unit_type, zealot.health, zealot.health_max = 73, 100, 100
        zealot.shield, zealot.shield_max = 50, 50
        env._obs.observation.raw_data.units[1].CopyFrom(zealot)
        data.units.update({74: "Stalker", 73: "Zealot"})
        data.unit_stats.update({
            74: SimpleNamespace(weapons=[SimpleNamespace(range=6, type=3)]),
            73: SimpleNamespace(weapons=[SimpleNamespace(range=0.1, type=1)])})
        payload = build_request(env, data, "jev-1.13.0", 1, previous_shields={100: 80})
        own = payload["state"]["allies"]["u0"]
        self.assertEqual((own["type"], own["effective_health"], own["max_shield"]), ("Stalker", 120, 80))
        self.assertEqual(own["shield_lost_since_previous_decision"], 20)
        relation = payload["state"]["relations"]["u0"]["e0"]
        self.assertTrue(relation["in_weapon_range"])
        self.assertFalse(relation["in_enemy_weapon_range"])
        self.assertIn("2s3z", payload["questions"]["u0"]["instructions"])
        # SMAC can permit an attack/approach order outside the real melee range.
        stalker.unit_type = 73
        payload = build_request(env, data, "jev-1.13.0", 1)
        relation = payload["state"]["relations"]["u0"]["e0"]
        self.assertFalse(relation["in_weapon_range"])
        self.assertTrue(relation["smac_attack_available"])
        self.assertIn("6", payload["questions"]["u0"]["criteria"])

    def test_dead_units_do_not_contribute_stale_shields(self):
        env, data = fixture()
        env.agents[1].shield = 80
        env.agents[1].shield_max = 80
        payload = build_request(env, data, "jev-1.13.0", 1)
        self.assertEqual(payload["state"]["allies"]["u1"]["effective_health"], 0)
        self.assertEqual(payload["state"]["allies"]["u1"]["shield"], 0)
        self.assertEqual(battle_state(env)["allied_shield"], 0)


class ExtendedMapTests(unittest.TestCase):
    def environment(self):
        env = JevMicroEnv.__new__(JevMicroEnv)
        env.map_type, env.map_name = 'MMM', 'MMM'
        env.use_ability, env.debug = False, False
        env.padding, env.n_actions_no_attack, env.n_actions = 9, 6, 15
        env._move_amount, env.map_x, env.map_y = 2, 32, 32
        env._step_mul, env._episode_steps, env.episode_limit = 8, 0, 150
        marine, mechanical, medivac = unit(10, 1, 10, health=20), unit(11, 1, 11, health=20), unit(12, 1, 12)
        mechanical.unit_type, medivac.unit_type = 33, 54
        medivac.is_flying, medivac.energy = True, 50
        env.agents = {0: marine, 1: mechanical, 2: medivac}
        env.enemies = {0: unit(20, 2, 14)}
        env._obs = sc.ResponseObservation()
        env._obs.observation.raw_data.units.extend([*env.agents.values(), *env.enemies.values()])
        env.micro_data = SimpleNamespace(units={48:'Marine', 33:'SiegeTank', 54:'Medivac'}, unit_stats={
            48: data_pb2.UnitTypeData(weapons=[data_pb2.Weapon(type=3, range=5)], attributes=[data_pb2.Biological]),
            33: data_pb2.UnitTypeData(weapons=[data_pb2.Weapon(type=1, range=7)], attributes=[data_pb2.Mechanical]),
            54: data_pb2.UnitTypeData(attributes=[data_pb2.Mechanical])})
        env.can_move = lambda u, d: True
        env._init_ally_unit_types(33)
        return env

    def test_medivac_heal_mask_and_real_command_target_ally(self):
        env = self.environment()
        self.assertEqual(env.medivac_id, 54)
        mask = env.get_avail_agent_actions(2)
        self.assertEqual(mask[6:9], [1, 0, 0])
        payload = build_request(env, env.micro_data, 'jev-1.13.0', 1)
        self.assertEqual(payload['state']['heal_candidates']['u2']['6']['target'], 'u0')
        self.assertFalse(payload['state']['relations']['u2']['e0']['smac_attack_available'])
        command = env.get_agent_action(2, 6).action_raw.unit_command
        self.assertEqual(command.ability_id, 386)
        self.assertEqual(command.target_unit_tag, env.agents[0].tag)
        env.agents[2].orders.add(ability_id=386, target_unit_tag=env.agents[0].tag)
        self.assertIsNone(env.get_agent_action(2, 6))
        env.agents[0].health = env.agents[0].health_max
        self.assertEqual(env.get_avail_agent_actions(2)[6], 0)
        env.agents[0].health = 20
        env.agents[2].energy = 0
        self.assertEqual(env.get_avail_agent_actions(2)[6], 0)

    def test_ground_weapon_cannot_attack_flying_target(self):
        env = self.environment()
        env.enemies[0].is_flying = True
        self.assertEqual(env.get_avail_agent_actions(0)[6], 1)
        self.assertEqual(env.get_avail_agent_actions(1)[6], 0)

    def test_battlecruiser_missing_weapon_list_does_not_disable_attack(self):
        env = self.environment()
        env.agents[0].unit_type = 57
        env.micro_data.units[57] = 'Battlecruiser'
        env.micro_data.unit_stats[57] = data_pb2.UnitTypeData()
        self.assertEqual(env.get_avail_agent_actions(0)[6], 1)
        payload = build_request(env, env.micro_data, 'jev-1.13.0', 1)
        self.assertTrue(payload['state']['relations']['u0']['e0']['in_weapon_range'])

    def test_effect_attacks_remain_available_without_protocol_weapons(self):
        env = self.environment()
        for unit_type, name in [(80, 'VoidRay'), (2010, 'Baneling_RL')]:
            with self.subTest(unit=name):
                env.agents[0].unit_type = unit_type
                env.micro_data.units[unit_type] = name
                env.micro_data.unit_stats[unit_type] = data_pb2.UnitTypeData()
                env.enemies[0].is_flying = False
                self.assertEqual(env.get_avail_agent_actions(0)[6], 1)
                env.enemies[0].is_flying = True
                self.assertEqual(env.get_avail_agent_actions(0)[6], int(unit_type == 80))

    def test_large_batches_cover_actors_once_and_preserve_observation(self):
        for name, allies, enemies in [('pvt_large', 58, 30), ('2c_vs_64zg', 2, 64), ('mmmt_vs_zhb', 22, 46)]:
            with self.subTest(map=name):
                env, data = fixture()
                env.map_name = name
                env.agents = {i: unit(100+i, 1, 9 + i / 100) for i in range(allies)}
                env.enemies = {i: unit(200+i, 2, 14 + i / 100) for i in range(enemies)}
                del env._obs.observation.raw_data.units[:]
                env._obs.observation.raw_data.units.extend([*env.agents.values(), *env.enemies.values()])
                env.get_avail_agent_actions = lambda i: [0] + [1]*(5+max(allies,enemies))
                payload = build_request(env, data, 'jev-1.13.0', 1)
                original = copy.deepcopy(payload)
                batches = batch_requests(payload)
                self.assertGreaterEqual(len(batches), 1)
                actors = [actor for b in batches for actor in b['questions']]
                self.assertEqual(len(actors), allies)
                self.assertEqual(set(actors), set(payload['questions']))
                for batch in batches:
                    size = lambda v: len(json.dumps(v, ensure_ascii=True, separators=(',',':')).encode())
                    self.assertLessEqual(size(batch['state']) + max(size(q) for q in batch['questions'].values()), 30000)
                    self.assertLessEqual(size(batch), 56000)
                    self.assertEqual(set(batch['state']['visible_enemies']), set(payload['state']['visible_enemies']))
                    self.assertEqual(set(batch['state']['allies']), set(payload['state']['allies']))
                self.assertEqual(payload, original)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.payload = {"model": "jev-1.13.0", "state": {}, "questions": {
            "u0": {"type": "choice", "instructions": "Control u0", "criteria": {"1": "Stop", "6": "Attack e0"}},
            "u1": {"type": "choice", "instructions": "Control u1", "criteria": {"1": "Stop"}}}}
        self.response = {"model": "jev-1.13.0", "answers": {
            "u0": {"type": "choice", "choice": "6", "confidence": 0.8, "probabilities": {"1": 0.1, "6": 0.9}},
            "u1": {"type": "choice", "choice": "1", "confidence": 1, "probabilities": {"1": 1}}},
            "usage": {"input_tokens": 100, "output_tokens": 20}}

    async def test_one_native_request_for_all_units(self):
        calls = []
        def handle(request):
            calls.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/v1/systemone")
            return httpx.Response(200, json=self.response)
        client = JevClient("dummy", transport=httpx.MockTransport(handle))
        try:
            result = await client.choose(self.payload)
            self.assertEqual(result["answers"]["u0"]["choice"], "6")
            self.assertEqual(calls, [self.payload])
        finally:
            await client.close()

    def test_reject_partial_or_illegal_answers_and_bad_probabilities(self):
        bad_cases = []
        data = copy.deepcopy(self.response)
        del data["answers"]["u1"]
        bad_cases.append(data)
        data = copy.deepcopy(self.response)
        data["answers"]["u0"]["choice"] = "99"
        bad_cases.append(data)
        data = copy.deepcopy(self.response)
        data["answers"]["u0"]["probabilities"]["6"] = float("nan")
        bad_cases.append(data)
        for data in bad_cases:
            with self.subTest(data=data), self.assertRaises(JevError):
                validate_response(data, self.payload)

    async def test_total_timeout(self):
        async def handle(request):
            await asyncio.sleep(0.1)
            return httpx.Response(200, json=self.response)
        client = JevClient("dummy", timeout=0.01, transport=httpx.MockTransport(handle))
        try:
            with self.assertRaisesRegex(JevError, "request_timeout"):
                await client.choose(self.payload)
        finally:
            await client.close()

    async def test_http_error_never_exposes_response_body_or_key(self):
        client = JevClient("test-private-value", transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="test-private-value")))
        try:
            with self.assertRaises(JevError) as caught:
                await client.choose(self.payload)
            self.assertEqual(str(caught.exception), "http_401")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
