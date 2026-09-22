"""Regressions for observed masks and multi-unit / upgrade completion semantics."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import unittest

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.ability_id import AbilityId as A
from sc2.ids.upgrade_id import UpgradeId
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.units import Units

import test_jev_protoss as support
from sc2_rl_agent.starcraftenv_test.agent.jev_agent import Decision
from sc2_rl_agent.starcraftenv_test.agent.jev_agent import JevError


class CommandUnit(support.FakeUnit):
    def __init__(self, bot, tag, kind, position=(10, 10)):
        super().__init__(tag, kind, position)
        self.bot = bot
        self.orders = []

    def __call__(self, ability, target=None):
        super().__call__(ability, target)
        self.bot.actions.append(SimpleNamespace(ability=ability, target=target, unit=self, queue=False))
        self.bot.unit_tags_received_action.add(self.tag)


class ActionCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await support.ProtossAdapterTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await support.ProtossAdapterTests.asyncTearDown(self)

    def templars(self, kinds=(U.HIGHTEMPLAR, U.HIGHTEMPLAR)):
        units = [CommandUnit(self.bot, 10+i, kind, (15+i*2, 15)) for i, kind in enumerate(kinds)]
        self.bot.units = Units([self.probe, *units], self.bot)
        return units

    async def merge(self):
        templars = self.templars()
        choices, _ = await self.bot.available_actions()
        self.assertIn(18, choices)  # Engine omits the multi-unit ability from queries.
        await self.bot._execute_decision(Decision(1, 0, 0, 18, {}, 100))
        self.assertEqual(len(self.bot.actions), 2)
        self.assertEqual(self.bot._production_orders[1]['producer_tags'], [10, 11])
        return templars

    async def test_merge_without_single_unit_ability_and_prevent_reuse(self):
        await self.merge()
        self.bot.unit_tags_received_action.clear()
        choices, blocked = await self.bot.available_actions()
        self.assertNotIn(18, choices)
        self.assertEqual(blocked['18'], 'need_two_available_templars')
        self.assertEqual(self.bot._count_with_pending(U.ARCHON), 1)

    async def test_mixed_and_dark_templar_pairs(self):
        for kinds in [(U.HIGHTEMPLAR, U.DARKTEMPLAR), (U.DARKTEMPLAR, U.DARKTEMPLAR)]:
            with self.subTest(kinds=kinds):
                self.templars(kinds)
                choices, _ = await self.bot.available_actions()
                self.assertIn(18, choices)

    async def test_hallucinations_and_existing_morph_orders_are_excluded(self):
        a, b = self.templars()
        b.is_hallucination = True
        self.assertIsNone(self.bot._archon_pair())
        b.is_hallucination = False
        a.orders = [SimpleNamespace(ability=SimpleNamespace(id=A.MORPH_ARCHON))]
        self.assertIsNone(self.bot._archon_pair())

    async def test_army_control_cannot_interrupt_approaching_merge_pair(self):
        templars = await self.merge()
        for u in templars:
            u.can_attack = True
        self.bot.unit_tags_received_action.clear()
        self.bot._issue_army_intent(include_busy=True)
        self.assertEqual([len(u.commands) for u in templars], [1, 1])

    async def test_unfinished_archon_is_started_and_counted_once_until_ready(self):
        await self.merge()
        archon = CommandUnit(self.bot, 20, U.ARCHON)
        archon.is_ready = False
        archon.can_attack = True
        self.bot.units = Units([self.probe, archon], self.bot)
        await self.bot.on_unit_created(archon)
        self.bot._consume_engine_feedback()
        self.assertEqual(self.bot._production_orders[1]['phase'], 'started')
        self.assertEqual(self.bot._count_with_pending(U.ARCHON), 1)
        self.assertFalse(self.bot._combat_units())
        archon.is_ready = True
        self.bot._consume_engine_feedback()
        self.assertNotIn(1, self.bot._production_orders)
        self.assertEqual(self.bot._count_with_pending(U.ARCHON), 1)

    async def test_error_on_second_templar_releases_pair(self):
        await self.merge()
        self.bot.state.action_errors = [SimpleNamespace(unit_tag=11, ability_id=A.MORPH_ARCHON.value, result=44)]
        self.bot._consume_engine_feedback()
        self.assertFalse(self.bot._morph_reserved_tags())
        self.assertEqual(self.bot.recent_outcomes[-1]['phase'], 'failed')

    async def test_templar_killed_before_merge_does_not_lock_survivor(self):
        a, _ = await self.merge()
        self.bot.units = Units([self.probe, a], self.bot)
        self.bot._consume_engine_feedback()
        self.assertFalse(self.bot._production_orders)
        self.assertIn('templar_lost_before_merge', str(self.bot.recent_outcomes[-1]))

    async def test_assimilator_mask_and_execution_share_ready_base_geysers(self):
        base = support.FakeUnit(4, U.NEXUS, (35, 35))
        base.is_ready = False
        geyser = support.FakeUnit(5, U.VESPENEGEYSER, (37, 35))
        self.bot.townhalls = Units([self.nexus, base], self.bot)
        self.bot.vespene_geyser = Units([geyser], self.bot)
        self.bot.can_afford = Mock(return_value=True)
        self.bot._abilities = {self.probe.tag: {TRAIN_INFO[U.PROBE][U.ASSIMILATOR]['ability']}}
        self.assertEqual(self.bot._build_reason(U.ASSIMILATOR, self.probe), 'no_free_geyser_at_ready_base')
        base.is_ready = True
        self.assertIsNone(self.bot._build_reason(U.ASSIMILATOR, self.probe))
        self.bot.select_build_worker = Mock(return_value=self.probe)
        self.bot.build = AsyncMock(return_value=True)
        await self.bot._build_one(20, U.ASSIMILATOR)
        self.bot.build.assert_awaited_once_with(U.ASSIMILATOR, geyser, build_worker=self.probe)

    async def test_chronoboost_busy_nexus_at_zero_free_supply(self):
        nexus = CommandUnit(self.bot, 2, U.NEXUS)
        nexus.is_idle = False
        self.bot.townhalls = self.bot.structures = Units([nexus], self.bot)
        self.bot.supply_left = 0
        self.bot.get_available_abilities = AsyncMock(side_effect=lambda units, **kwargs: [
            [A.EFFECT_CHRONOBOOSTENERGYCOST] if u.type_id == U.NEXUS else [] for u in units])
        choices, _ = await self.bot.available_actions()
        self.assertIn(66, choices)
        await self.bot._execute_decision(Decision(1, 0, 0, 66, {}, 100))
        self.assertEqual(self.bot._production_orders[1]['order_type'], 'chronoboost')
        nexus.has_buff = Mock(return_value=True)
        self.bot._consume_engine_feedback()
        self.assertFalse(self.bot._production_orders)

    async def test_upgrade_completion_is_distinct_from_order_submission(self):
        upgrade = UpgradeId.PROTOSSGROUNDARMORSLEVEL2
        ability = A.FORGERESEARCH_PROTOSSGROUNDARMORLEVEL2
        forge = CommandUnit(self.bot, 3, U.FORGE)
        self.bot.structures = Units([self.nexus, forge], self.bot)
        self.bot._track_production_order(Decision(1, 0, 0, 48, {}, 100),
                                        [SimpleNamespace(unit=forge, ability=ability, target=None)])
        self.bot._consume_engine_feedback()
        self.assertEqual(self.bot._production_orders[1]['phase'], 'submitted')
        forge.orders = [SimpleNamespace(ability=SimpleNamespace(id=ability))]
        self.bot._consume_engine_feedback()
        self.assertEqual(self.bot._production_orders[1]['phase'], 'started')
        await self.bot.on_upgrade_complete(upgrade)
        self.assertFalse(self.bot._production_orders)

    async def test_destroyed_researcher_reports_failure(self):
        forge = CommandUnit(self.bot, 3, U.FORGE)
        self.bot._track_production_order(Decision(1, 0, 0, 48, {}, 100),
            [SimpleNamespace(unit=forge, ability=A.FORGERESEARCH_PROTOSSGROUNDARMORLEVEL2, target=None)])
        await self.bot.on_unit_destroyed(forge.tag)
        self.assertIn('researcher_destroyed', str(self.bot.recent_outcomes[-1]))

    async def test_carrier_without_weapon_data_and_legacy_omissions_join_army(self):
        carrier = CommandUnit(self.bot, 10, U.CARRIER)
        sentry = CommandUnit(self.bot, 11, U.SENTRY)
        mothership = CommandUnit(self.bot, 12, U.MOTHERSHIP)
        mothership.can_attack = True
        oracle = CommandUnit(self.bot, 13, U.ORACLE)
        oracle.can_attack = True  # SDK special-cases Oracle even with its beam off.
        self.bot.units = Units([self.probe, carrier, sentry, mothership, oracle], self.bot)
        self.assertEqual(self.bot._combat_units().tags, {10, 11, 12})
        self.assertEqual(self.bot._ready_army_supply(), 3)

    async def test_spell_support_follows_army_without_counting_as_ready_attack_supply(self):
        army = CommandUnit(self.bot, 10, U.ZEALOT, (70, 70))
        disruptor = CommandUnit(self.bot, 11, U.DISRUPTOR, (10, 10))
        self.bot.units = Units([self.probe, army, disruptor], self.bot)
        self.bot._maintain_scouts_and_detection()
        self.assertEqual(disruptor.commands[0][0], 'move')
        self.assertEqual(self.bot._ready_army_supply(), 2)

    async def test_disabled_model_aborts_before_more_actions_or_local_maintenance(self):
        self.bot.scheduler.disabled = True
        self.bot.scheduler.disabled_status = 402
        self.bot._maintain_local_behaviors = AsyncMock()
        with self.assertRaisesRegex(JevError, 'http_402'):
            await self.bot.on_step(0)
        self.bot._maintain_local_behaviors.assert_not_awaited()
        self.assertEqual(self.bot.log.failure['type'], 'JevError')
        self.assertFalse(self.bot.actions)


if __name__ == '__main__':
    unittest.main()
