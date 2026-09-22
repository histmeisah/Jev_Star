"""Adapter regression tests with a small fake SC2 world (no running game required)."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from sc2.data import Race
from sc2.game_data import GameData
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.units import Units
from s2clientprotocol import sc2api_pb2

from sc2_rl_agent.starcraftenv_test.agent.jev_agent import Decision, JevClient, QUESTION
from sc2_rl_agent.starcraftenv_test.env.bot.jev_protoss_bot import JevProtossBot


class FakeUnit:
    def __init__(self, tag, kind, position=(10, 10)):
        self.tag, self.type_id, self.position = tag, kind, Point2(position)
        self._proto = SimpleNamespace(pos=SimpleNamespace(x=self.position.x, y=self.position.y))
        self.is_ready = self.is_idle = self.is_powered = self.is_visible = True
        self.is_gathering = kind == U.PROBE
        self.can_attack = kind == U.ZEALOT
        self.energy = 100
        self.assigned_harvesters, self.ideal_harvesters = 12, 16
        self.health_percentage = 1.0
        self.health = self.shield = 100
        self.build_progress = 1.0
        self.commands = []
        self.train = Mock()
        self.warp_in = Mock()

    def __call__(self, ability, target=None):
        self.commands.append((ability, target))

    def has_buff(self, buff):
        return False

    def move(self, target):
        self.commands.append(("move", target))

    def attack(self, target):
        self.commands.append(("attack", target))

    def distance_to(self, target):
        return self.position.distance_to(target.position if hasattr(target, "position") else target)


class FakeBases(Units):
    def closest_to(self, position):
        return min(self, key=lambda unit: unit.distance_to(position))


class ProtossAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        client = JevClient("test-only", transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        self.bot = JevProtossBot(client, Path(self.directory.name))
        self.bot._initialize_variables()
        self.bot.state = SimpleNamespace(game_loop=0)
        self.bot.enemy_race = Race.Zerg
        self.bot.game_info = SimpleNamespace(player_start_location=Point2((10, 10)),
                                             start_locations=[Point2((100, 100))], map_center=Point2((55, 55)))
        self.bot.game_data = SimpleNamespace(abilities={}, upgrades={})
        self.probe, self.nexus = FakeUnit(1, U.PROBE), FakeUnit(2, U.NEXUS)
        self.bot.units = self.bot.workers = Units([self.probe], self.bot)
        self.bot.structures = self.bot.townhalls = FakeBases([self.nexus], self.bot)
        self.bot.already_pending = Mock(return_value=0)
        self.bot.already_pending_upgrade = Mock(return_value=0)
        self.bot.supply_workers = 1
        self.bot.calculate_supply_cost = Mock(side_effect=lambda kind: 2 if kind == U.ZEALOT else 1)
        self.bot.is_visible = Mock(return_value=False)
        self.bot.can_afford = Mock(side_effect=lambda item: item == U.PROBE and self.bot.minerals >= 50)
        self.bot.get_available_abilities = AsyncMock(side_effect=lambda units, **kwargs: [
            [TRAIN_INFO[U.NEXUS][U.PROBE]["ability"]] if u.type_id == U.NEXUS else [] for u in units])

    async def asyncTearDown(self):
        await self.bot.shutdown("test")
        self.directory.cleanup()

    async def test_opening_observation_and_mask_use_real_registry(self):
        choices, blocked = await self.bot.available_actions()
        self.assertEqual(len(self.bot.action_dict), 73)
        self.assertEqual(choices, {0: "TRAIN PROBE", 71: "EMPTY ACTION"})
        self.assertIn("19", blocked)
        snapshot = self.bot._snapshot()
        json.dumps(snapshot, allow_nan=False)
        self.assertEqual(snapshot["resource"]["mineral"], 50)
        self.assertEqual(snapshot["ready_idle_producers"], {"NEXUS": 1})

    async def test_busy_nexus_is_not_a_training_candidate(self):
        self.nexus.is_idle = False
        choices, _ = await self.bot.available_actions()
        self.assertNotIn(0, choices)
        self.assertIn(71, choices)

    async def test_obsolete_upgrade_record_without_ability_is_masked(self):
        data = sc2api_pb2.ResponseData()
        data.upgrades.add(upgrade_id=UpgradeId.TEMPESTGROUNDATTACKUPGRADE.value,
                          name="TempestGroundAttackUpgrade", ability_id=0)
        self.bot.game_data = GameData(data)
        self.bot.already_pending_upgrade.side_effect = AssertionError("Must not query an unavailable upgrade")
        choices, blocked = await self.bot.available_actions()
        self.assertNotIn(59, choices)
        self.assertEqual(blocked["59"], "upgrade_not_in_game_version")
        self.assertIn(0, choices)

    async def test_resources_changed_during_request_prevent_execution(self):
        self.bot.minerals = 0
        self.bot.handle_action_0 = AsyncMock()
        await self.bot._execute_decision(Decision(1, 0, 0, 0, {}, 100))
        self.bot.handle_action_0.assert_not_awaited()
        self.assertEqual(self.bot._action_stats["invalidated_before_execution"], 1)

    async def test_training_dispatch_uses_checked_producer(self):
        ability = TRAIN_INFO[U.NEXUS][U.PROBE]["ability"]
        self.nexus.train.side_effect = lambda _: self.bot.actions.append(
            SimpleNamespace(ability=ability, unit=self.nexus))
        with contextlib.redirect_stdout(io.StringIO()):
            await self.bot._execute_decision(Decision(1, 0, 0, 0, {}, 100))
        self.nexus.train.assert_called_once_with(U.PROBE)
        self.assertEqual(self.bot.recent_outcomes[-1]["orders_submitted"], 1)
        self.assertEqual(self.bot.recent_outcomes[-1]["failures"], [])

    async def test_adept_warp_issues_one_command_and_no_false_failure(self):
        gate = FakeUnit(3, U.WARPGATE)
        self.bot.structures = Units([gate], self.bot)
        ability = TRAIN_INFO[U.WARPGATE][U.ADEPT]["ability"]
        self.bot._abilities = {gate.tag: {ability}}
        self.bot.find_placement = AsyncMock(return_value=Point2((11, 11)))
        await self.bot.warp_unit(2, U.ADEPT, Point2((10, 10)))
        gate.warp_in.assert_called_once_with(U.ADEPT, Point2((11, 11)))
        self.assertEqual(self.bot.find_placement.await_count, 1)
        self.assertEqual(self.bot.temp_failure_list, [])

    async def test_chronoboost_selects_busy_target_and_returns_after_success(self):
        self.nexus.is_idle = False
        self.bot._abilities = {self.nexus.tag: {AbilityId.EFFECT_CHRONOBOOSTENERGYCOST}}
        await self.bot.apply_chronoboost(66, U.NEXUS, 2, "base")
        self.assertEqual(self.nexus.commands, [(AbilityId.EFFECT_CHRONOBOOSTENERGYCOST, self.nexus)])
        self.assertEqual(self.bot.temp_failure_list, [])

    async def test_retreat_targets_own_base_instead_of_frontline_unit(self):
        army = FakeUnit(3, U.ZEALOT, (70, 70))
        self.bot.units = Units([self.probe, army], self.bot)
        self.bot.supply_army = 2
        await self.bot.handle_action_65()
        self.assertEqual(self.bot.army_intent, "retreat")
        self.assertEqual(army.commands, [("move", self.nexus.position)])

    async def test_worker_distribution_cannot_override_new_builder_command(self):
        self.bot.actions = [SimpleNamespace(unit=self.probe)]
        self.bot.distribute_workers = AsyncMock()
        await self.bot._maintain_local_behaviors()
        self.bot.distribute_workers.assert_not_awaited()

    async def test_upgrade_48_dispatches_armor_instead_of_weapons(self):
        self.bot.available_actions = AsyncMock(return_value=({48: self.bot.action_dict[48]}, {}))
        forge = FakeUnit(3, U.FORGE)
        forge.research = Mock()
        self.bot._producers = [forge]
        self.bot._has_ability = Mock(return_value=True)
        await self.bot._execute_decision(Decision(1, 0, 0, 48, {}, 100))
        forge.research.assert_called_once_with(UpgradeId.PROTOSSGROUNDARMORSLEVEL2)

    async def test_on_step_advances_with_pending_http_then_dispatches_once(self):
        release = asyncio.Event()
        requests = []

        async def delayed(request):
            requests.append(json.loads(request.content))
            await release.wait()
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {QUESTION: {
                "type": "choice", "choice": "0", "confidence": 0.9,
                "probabilities": {"0": 0.9, "71": 0.1},
            }}, "usage": {"input_tokens": 100, "output_tokens": 20}})

        await self.bot.scheduler.client.close()
        self.bot.scheduler.client = JevClient("test-only", transport=httpx.MockTransport(delayed))
        self.bot._maintain_local_behaviors = AsyncMock()
        ability = TRAIN_INFO[U.NEXUS][U.PROBE]["ability"]
        self.nexus.train.side_effect = lambda _: self.bot.actions.append(
            SimpleNamespace(ability=ability, unit=self.nexus))
        with contextlib.redirect_stdout(io.StringIO()):
            for iteration in range(10):
                self.bot.state.game_loop = iteration * 4
                await self.bot.on_step(iteration)
                await asyncio.sleep(0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(self.bot._steps, 10)
            self.assertEqual(self.bot._steps_while_inflight, 9)
            release.set()
            await self.bot.scheduler.task
            self.bot.state.game_loop = 40
            await self.bot.on_step(10)
            self.bot.state.game_loop = 44
            await self.bot.on_step(11)
        self.nexus.train.assert_called_once_with(U.PROBE)
        self.assertEqual(self.bot._action_stats["decisions_executed"], 1)


if __name__ == "__main__":
    unittest.main()
