"""Regressions for failures observed in the September 22 ladder replays."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.units import Units
from sc2.data import ActionResult

import test_jev_protoss as support
from test_jev_hierarchy import plan, catalog, resource
from sc2_rl_agent.starcraftenv_test.agent.astra_planner import validate_plan, PlannerError, codex_failure_category
from sc2_rl_agent.starcraftenv_test.agent.macro_contract import ACTION_LIMITS, primary_action
from sc2_rl_agent.starcraftenv_test.agent.strategic_policy import policy_reason, plan_progress
from sc2_rl_agent.starcraftenv_test.agent.jev_agent import Decision


class MacroRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await support.ProtossAdapterTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await support.ProtossAdapterTests.asyncTearDown(self)

    def army(self):
        unit = support.FakeUnit(10, U.ZEALOT, (70, 70))
        self.bot.units = Units([self.probe, unit], self.bot)
        self.bot.supply_army = 2
        return unit

    def enable_battery(self):
        pylon, core = support.FakeUnit(3, U.PYLON), support.FakeUnit(4, U.CYBERNETICSCORE)
        self.bot.structures = Units([self.nexus, pylon, core], self.bot)
        self.bot.can_afford = Mock(return_value=True)
        self.bot._refresh_abilities = AsyncMock(return_value=self.probe)
        ability = TRAIN_INFO[U.PROBE][U.SHIELDBATTERY]["ability"]
        self.bot._abilities = {self.probe.tag: {ability}}
        async def build(kind, near, **kwargs):
            self.bot.actions.append(SimpleNamespace(ability=ability, unit=self.probe, target=near))
            return True
        self.bot.build = AsyncMock(side_effect=build)
        return ability

    async def test_total_workers_not_visible_workers_controls_mask_and_state(self):
        self.bot.supply_workers = 77
        self.bot.already_pending = Mock(side_effect=lambda kind: int(kind == U.PROBE))
        state = self.bot._snapshot()
        self.assertEqual(state["resource"]["worker_supply"], 77)
        self.assertEqual(state["resource"]["visible_workers"], 1)
        self.assertEqual(state["unit"]["probe_count"], 77)
        self.assertEqual(self.bot._count_with_pending(U.PROBE), 78)
        choices, blocked = await self.bot.available_actions()
        self.assertNotIn(0, choices)
        self.assertEqual(blocked["0"], "unit_policy_limit")

    async def test_battery_without_forge_passes_mask_and_submits_construction(self):
        self.enable_battery()
        choices, _ = await self.bot.available_actions()
        self.assertIn(33, choices)
        await self.bot._execute_decision(Decision(1, 0, 0, 33, {}, 100))
        self.bot.build.assert_awaited_once()
        self.assertEqual(self.bot.recent_outcomes[-1]["orders_submitted"], 1)
        self.assertFalse(self.bot.recent_outcomes[-1]["failures"])

    async def test_failed_placement_reports_reason_and_backs_off(self):
        self.enable_battery()
        self.bot.build = AsyncMock(return_value=False)
        await self.bot._execute_decision(Decision(1, 0, 0, 33, {}, 100))
        self.assertEqual(self.bot._action_stats["executor_no_order"], 1)
        self.assertIn("no_valid_powered_placement", str(self.bot.recent_outcomes[-1]["failures"]))
        choices, blocked = await self.bot.available_actions()
        self.assertNotIn(33, choices)
        self.assertEqual(blocked["33"], "recent_executor_rejection")

    async def test_existing_four_batteries_do_not_hit_removed_legacy_limit(self):
        self.enable_battery()
        self.bot.structures.extend(support.FakeUnit(20 + i, U.SHIELDBATTERY) for i in range(4))
        choices, _ = await self.bot.available_actions()
        self.assertIn(33, choices)

    async def test_nexus_rebuild_with_worker_and_money_does_not_require_townhall(self):
        self.bot.townhalls = Units([], self.bot)
        self.bot.structures = Units([], self.bot)
        self.bot._abilities = {self.probe.tag: {TRAIN_INFO[U.PROBE][U.NEXUS]["ability"]}}
        self.bot.can_afford = Mock(return_value=True)
        self.assertIsNone(self.bot._build_reason(U.NEXUS, self.probe))
        position = Point2((30, 30))
        self.bot._expansion_positions_list = [position]
        self.bot.client = SimpleNamespace(query_pathing=AsyncMock(return_value=28))
        self.bot.select_build_worker = Mock(return_value=self.probe)
        self.bot.build = AsyncMock(return_value=True)
        await self.bot._build_one(21, U.NEXUS)
        self.bot.build.assert_awaited_once()

    async def test_gateway_count_includes_warpgates_and_pending_once(self):
        pending = support.FakeUnit(4, U.GATEWAY)
        pending.is_ready = False
        self.bot.structures = Units([support.FakeUnit(2, U.GATEWAY), support.FakeUnit(3, U.WARPGATE), pending], self.bot)
        self.bot.already_pending = Mock(side_effect=lambda kind: int(kind == U.GATEWAY))
        self.assertEqual(self.bot._count_with_pending(U.GATEWAY), 3)

    async def test_supply_forecast_can_allow_second_pending_pylon(self):
        self.bot.supply_cap, self.bot.supply_left = 100, 4
        gates = [support.FakeUnit(20 + i, U.WARPGATE) for i in range(5)]
        self.bot.structures = Units([self.nexus, *gates], self.bot)
        self.bot.already_pending = Mock(side_effect=lambda kind: int(kind == U.PYLON))
        self.bot._abilities = {self.probe.tag: {TRAIN_INFO[U.PROBE][U.PYLON]["ability"]}}
        self.bot.can_afford = Mock(return_value=True)
        self.assertTrue(self.bot._supply_forecast()["needs_supply"])
        self.assertIsNone(self.bot._build_reason(U.PYLON, self.probe))

    async def test_pylon_power_repair_is_available_at_200_supply(self):
        gate = support.FakeUnit(3, U.GATEWAY)
        gate.is_powered = False
        self.bot.structures = Units([self.nexus, gate], self.bot)
        self.bot.supply_cap, self.bot.supply_left = 200, 30
        self.bot._abilities = {self.probe.tag: {TRAIN_INFO[U.PROBE][U.PYLON]["ability"]}}
        self.bot.can_afford = Mock(return_value=True)
        self.assertIsNone(self.bot._build_reason(U.PYLON, self.probe))

    async def test_defend_restores_retreat_and_acknowledges_intent_without_fake_orders(self):
        army = self.army()
        await self.bot.handle_action_65()
        await self.bot._execute_decision(Decision(1, 0, 0, 72, {}, 100))
        self.assertEqual(self.bot.army_intent, "defend")
        self.assertEqual(army.commands[-1][0], "attack")
        self.assertTrue(self.bot.recent_outcomes[-1]["intent_applied"])
        self.assertEqual(self.bot._action_stats["executor_no_order"], 0)

    async def test_single_scout_worker_and_changeling_do_not_trigger_emergency(self):
        drone = support.FakeUnit(3, U.DRONE)
        changeling = support.FakeUnit(4, U.CHANGELINGZEALOT)
        drone.can_attack = changeling.can_attack = True
        self.bot.enemy_units = Units([drone, changeling], self.bot)
        self.assertFalse(self.bot._emergency())
        self.bot._update_navigation()
        self.nexus.health -= 5
        self.bot.state.game_loop = 24
        self.bot._update_navigation()
        self.assertTrue(self.bot._emergency())

    async def test_worker_rush_is_not_filtered_out(self):
        drones = [support.FakeUnit(3 + i, U.DRONE) for i in range(3)]
        for drone in drones: drone.can_attack = True
        self.bot.enemy_units = Units(drones, self.bot)
        self.assertTrue(self.bot._emergency())

    async def test_enemy_building_memory_persists_under_fog_and_clears_with_vision(self):
        enemy = support.FakeUnit(20, U.HATCHERY, (100, 100))
        self.bot.enemy_structures = Units([enemy], self.bot)
        self.bot._update_navigation()
        self.bot.enemy_structures = Units([], self.bot)
        self.bot.state.game_loop = 24
        self.bot._update_navigation()
        self.assertIn(enemy.tag, self.bot._known_enemy_buildings)
        self.bot.is_visible = Mock(return_value=True)
        self.bot.state.game_loop = 48
        self.bot._update_navigation()
        self.assertNotIn(enemy.tag, self.bot._known_enemy_buildings)

    async def test_cleared_main_causes_expansion_search_and_busy_army_retarget(self):
        army = self.army()
        army.is_idle = False
        self.bot._set_search_sites([Point2((10, 10)), Point2((80, 80)), Point2((100, 100))])
        self.bot.is_visible = Mock(side_effect=lambda p: p.distance_to(Point2((100, 100))) < 8)
        self.bot._update_navigation()
        self.bot.army_intent = "attack"
        self.bot._army_target_id = "enemy_start"
        self.bot._unit_destinations[army.tag] = Point2((100, 100))
        self.bot._issue_army_intent()
        self.assertEqual(army.commands[-1], ("attack", Point2((80, 80))))

    async def test_one_observer_stays_with_army_and_second_can_scout(self):
        army = self.army()
        observer = support.FakeUnit(11, U.OBSERVER)
        self.bot.units = Units([army, observer], self.bot)
        self.assertFalse(self.bot._scout_candidates(U.OBSERVER))
        second = support.FakeUnit(12, U.OBSERVER)
        self.bot.units.append(second)
        self.assertEqual(self.bot._scout_candidates(U.OBSERVER).tags, {12})
        self.bot._maintain_scouts_and_detection()
        self.assertTrue(observer.commands)

    async def test_building_order_lifecycle_has_start_and_completion(self):
        self.enable_battery()
        await self.bot._execute_decision(Decision(1, 0, 0, 33, {}, 100))
        order = self.bot._production_orders[1]
        building = support.FakeUnit(50, U.SHIELDBATTERY, order["position"])
        await self.bot.on_building_construction_started(building)
        self.assertEqual(order["phase"], "started")
        await self.bot.on_building_construction_complete(building)
        self.assertNotIn(1, self.bot._production_orders)
        events = [json.loads(line) for line in (self.bot.output_dir / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([e['phase'] for e in events if e['event'] == 'order_lifecycle'], ['started', 'completed'])

    async def test_engine_rejection_is_matched_to_submitted_order(self):
        ability = self.enable_battery()
        await self.bot._execute_decision(Decision(1, 0, 0, 33, {}, 100))
        self.bot.state.action_errors = [SimpleNamespace(unit_tag=self.probe.tag, ability_id=ability.value, result=44)]
        self.bot._notify_execution_outcome = Mock()
        self.bot._consume_engine_feedback()
        self.assertNotIn(1, self.bot._production_orders)
        self.assertGreater(self.bot.cooldowns[33], self.bot.time)
        self.assertEqual(self.bot.recent_outcomes[-1]['phase'], 'failed')
        self.bot._notify_execution_outcome.assert_called_once()

    async def test_cost_ignoring_query_cannot_authorize_uncharged_warpgate(self):
        gate = support.FakeUnit(3, U.WARPGATE)
        self.bot.structures = Units([self.nexus, gate], self.bot)
        self.bot.can_afford = Mock(return_value=True)
        ability = TRAIN_INFO[U.WARPGATE][U.STALKER]['ability']
        self.bot.get_available_abilities = AsyncMock(side_effect=lambda units, ignore_resource_requirements=False: [
            [ability] if u.type_id == U.WARPGATE and ignore_resource_requirements else [] for u in units])
        choices, blocked = await self.bot.available_actions()
        self.assertNotIn(3, choices)
        self.assertEqual(blocked['3'], 'no_ready_producer_with_available_ability')
        self.assertIsNone(self.bot._train_reason(U.STALKER, ignore_resources=True))
        self.assertFalse(self.bot._has_ability(gate, ability))

    async def test_action_response_acknowledgement_preserves_sdk_return_contract(self):
        ability = self.enable_battery()
        await self.bot._execute_decision(Decision(1, 0, 0, 33, {}, 100))
        command = self.bot.actions[0]
        command.combining_tuple = (ability, command.target, False, False)
        original = AsyncMock(return_value=[ActionResult.Success])
        self.bot.client = SimpleNamespace(actions=original)
        self.bot._install_action_feedback()
        result = await self.bot.client.actions([command])
        self.assertEqual(result, [])
        self.assertEqual(self.bot._production_orders[1]['phase'], 'accepted')
        original.assert_awaited_once_with([command], return_successes=True)

    async def test_missing_engine_response_is_not_reported_as_acceptance(self):
        ability = self.enable_battery()
        await self.bot._execute_decision(Decision(1, 0, 0, 33, {}, 100))
        command = self.bot.actions[0]
        command.combining_tuple = (ability, command.target, False, False)
        self.bot.client = SimpleNamespace(actions=AsyncMock(return_value=[]))
        self.bot._install_action_feedback()
        await self.bot.client.actions([command])
        self.assertEqual(self.bot._production_orders[1]['phase'], 'submitted')


class MacroPlanRegressionTests(unittest.TestCase):
    def test_plan_targets_use_same_limits_as_executor(self):
        p = plan()
        p['goals'] = [{'action_id': 13, 'target': ACTION_LIMITS[13] + 1}]
        p['allowed_spending_actions'] = [13]
        p['reserve_for_action'] = None
        with self.assertRaisesRegex(PlannerError, 'executor_limit'):
            validate_plan(p)

    def test_reservation_for_impossible_action_is_suspended(self):
        c = catalog()
        c['21']['reservation_blocked'] = 'no_available_builder'
        self.assertEqual(plan_progress(plan(), c, resource())['reserved_minerals'], 0)
        self.assertIsNone(policy_reason(1, plan(), c, resource(), 'defend', 0, 100, False))

    def test_attack_uses_ready_combat_supply_instead_of_pending_supply(self):
        r = resource()
        r.update(army_supply=100, ready_army_supply=2)
        self.assertEqual(policy_reason(64, plan(), catalog(), r, 'defend', 0, 100, False), 'plan_attack_not_ready')

    def test_army_order_precedes_production_and_defend_can_restore_retreat(self):
        p = plan()
        p.update(priority_action=21, production_priority=[21])
        self.assertEqual(primary_action(p, {64: 'attack', 21: 'Nexus'}, 'defend', 40)[0], 64)
        p['army_posture'] = 'defend'
        self.assertEqual(policy_reason(72, p, catalog(), resource(), 'retreat', 99, 100, False), 'plan_hold_army_intent')
        self.assertIsNone(policy_reason(72, p, catalog(), resource(), 'retreat', 70, 100, False))
        p['accepted_game_seconds'] = 100
        self.assertIsNone(policy_reason(72, p, catalog(), resource(), 'retreat', 99, 100, False))
        self.assertEqual(primary_action(p, {72: 'defend', 21: 'Nexus'}, 'retreat', 40)[0], 72)

    def test_defense_cannot_immediately_repeat_retreat_on_same_threat(self):
        p = plan()
        p['army_posture'] = 'defend'
        self.assertEqual(policy_reason(65, p, catalog(), resource(), 'defend', 99, 100, True), 'plan_hold_army_intent')

    def test_acknowledged_one_off_priority_does_not_starve_next_production(self):
        p = plan()
        p.update(priority_action=67, production_priority=[21])
        choices = {67: 'Chronoboost', 21: 'Nexus'}
        self.assertEqual(primary_action(p, choices, 'attack', 40)[0], 67)
        self.assertEqual(primary_action(p, choices, 'attack', 40, acknowledged_actions={67})[0], 21)

    def test_unknown_navigation_target_and_conflicting_priority_are_rejected(self):
        p = plan()
        p['army_target_id'] = 'enemy_not_observed'
        with self.assertRaisesRegex(PlannerError, 'army_target'):
            validate_plan(p, target_ids={'home', 'expansion_1'})
        p['army_target_id'] = None
        p['priority_action'] = 72
        with self.assertRaisesRegex(PlannerError, 'conflicts'):
            validate_plan(p)

    def test_codex_error_classification_never_returns_arbitrary_output(self):
        self.assertEqual(codex_failure_category('account-secret', 'rate limit exceeded token=secret'), 'rate_limit')
        self.assertEqual(codex_failure_category('account-secret', 'unknown error account-secret'), 'unknown')


if __name__ == '__main__':
    unittest.main()
