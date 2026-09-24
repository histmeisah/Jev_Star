"""Army command debounce: real intent transitions, live masks and escape path."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from sc2_rl_agent.starcraftenv_test.agent.jev_agent import Decision, JevClient
from sc2_rl_agent.starcraftenv_test.env.bot.hierarchical_protoss_bot import HierarchicalProtossBot
from sc2_rl_agent.starcraftenv_test.env.bot.jev_protoss_bot import JevProtossBot
from sc2_rl_agent.starcraftenv_test.run_jev import nonnegative_float


class ArmyCommandGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        client = JevClient('test-only', transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        planner = SimpleNamespace(close=AsyncMock(), model='test-planner')
        self.bot = HierarchicalProtossBot(client, Path(self.directory.name),
                                         planner_client=planner, plan_mode='advisory')
        self.bot._initialize_variables()
        self.set_time(100)
        self.bot._issue_army_intent = Mock()
        self.bot._emergency = Mock(return_value=True)
        self.bot._train_one = AsyncMock()
        self.bot._track_production_order = Mock()
        self.choices = {1: 'TRAIN ZEALOT', 64: 'MULTI-ATTACK', 65: 'MULTI-RETREAT',
                        71: 'EMPTY ACTION', 72: 'MULTI-DEFEND'}
        # Isolate the extra rule from SC2 affordance checks, which have their own
        # adapter tests. The execution path below still reruns this live mask.
        self.base = patch.object(JevProtossBot, 'available_actions',
                                 new=AsyncMock(side_effect=lambda: (self.choices.copy(), {})))
        self.base.start()

    async def asyncTearDown(self):
        self.base.stop()
        await self.bot.shutdown('test')
        self.directory.cleanup()

    def set_time(self, seconds):
        self.bot.state = SimpleNamespace(game_loop=round(seconds * 22.4))

    async def execute(self, action, request_id=1):
        await self.bot._execute_decision(Decision(request_id, self.bot.state.game_loop,
                                                 0, action, {}, 1))

    async def test_opening_defense_does_not_delay_first_attack(self):
        choices, blocked = await self.bot.available_actions()
        self.assertEqual(choices, self.choices)
        self.assertFalse(blocked)
        await self.execute(64)
        self.assertEqual(self.bot.army_intent, 'attack')
        self.assertEqual(self.bot._guard_attack_started_at, 100)

    async def test_attack_blocks_defend_for_twenty_seconds_even_with_existing_alarm(self):
        await self.execute(64)
        self.set_time(101)
        choices, blocked = await self.bot.available_actions()
        self.assertNotIn(72, choices)
        self.assertEqual(blocked['72'], 'army_command_attack_hold')
        self.assertIn(65, choices)
        self.assertIn(1, choices)
        self.assertIn(71, choices)
        self.assertEqual(self.bot._base_available_actions, sorted(self.choices))
        self.assertFalse(self.bot._policy_blocks)
        self.set_time(120)
        choices, blocked = await self.bot.available_actions()
        self.assertIn(72, choices)
        self.assertFalse(blocked)

    async def test_retreat_is_immediate_and_defend_cannot_bypass_reengage_cooldown(self):
        await self.execute(64)
        self.set_time(101)
        await self.execute(65, 2)
        expected_resume_time = self.bot.time + 20
        self.assertEqual(self.bot.army_intent, 'retreat')
        choices, blocked = await self.bot.available_actions()
        self.assertNotIn(64, choices)
        self.assertEqual(blocked['64'], 'army_command_reengage_cooldown')
        self.assertIn(72, choices)
        self.set_time(102)
        await self.execute(72, 3)
        self.assertEqual(self.bot.army_intent, 'defend')
        self.assertEqual(self.bot._guard_reengage_after, expected_resume_time)
        self.set_time(120)
        self.assertNotIn(64, (await self.bot.available_actions())[0])
        self.set_time(121)
        self.assertIn(64, (await self.bot.available_actions())[0])

    async def test_normal_defend_after_attack_also_holds_reengagement(self):
        await self.execute(64)
        self.set_time(120)
        await self.execute(72, 2)
        self.assertEqual(self.bot.army_intent, 'defend')
        self.set_time(139)
        self.assertNotIn(64, (await self.bot.available_actions())[0])
        self.set_time(140)
        self.assertIn(64, (await self.bot.available_actions())[0])

    async def test_wait_production_and_same_intent_do_not_restart_attack_timer(self):
        await self.execute(64)
        self.set_time(105)
        await self.execute(71, 2)
        self.set_time(110)
        await self.execute(1, 3)
        self.bot._train_one.assert_awaited_once()
        self.set_time(115)
        self.bot._set_army_intent('attack')  # Same mission, including a retarget.
        self.assertEqual(self.bot._guard_attack_started_at, 100)
        self.set_time(120)
        self.assertIn(72, (await self.bot.available_actions())[0])

    async def test_new_plan_and_expiry_neither_reset_nor_remove_guard(self):
        await self.execute(64)
        self.set_time(102)
        self.bot.planner.active = {'plan_id': 8, 'expires_game_seconds': 110,
                                   'army_posture': 'defend', 'min_posture_seconds': 60}
        self.assertNotIn(72, (await self.bot.available_actions())[0])
        self.bot.planner.active = None
        self.set_time(119)
        self.assertNotIn(72, (await self.bot.available_actions())[0])
        self.assertEqual(self.bot._guard_attack_started_at, 100)

    async def test_execution_revalidates_and_cannot_send_a_blocked_defend(self):
        # A defend decision selected before a newly applied attack is now invalid.
        await self.execute(64)
        self.bot._issue_army_intent.reset_mock()
        self.set_time(101)
        await self.execute(72, 2)
        self.assertEqual(self.bot.army_intent, 'attack')
        self.bot._issue_army_intent.assert_not_called()
        self.assertEqual(self.bot._action_stats['invalidated_before_execution'], 1)
        self.assertEqual(self.bot._guard_attack_started_at, 100)
        self.assertEqual(self.bot.recent_outcomes[-1]['action_id'], 64)

    async def test_zero_restores_original_candidate_set(self):
        self.bot.advisory_posture_hold = 0
        await self.execute(64)
        self.set_time(101)
        choices, blocked = await self.bot.available_actions()
        self.assertEqual(choices, self.choices)
        self.assertFalse(blocked)
        self.assertFalse(self.bot._army_command_guard()['enabled'])
        await self.execute(72, 2)
        self.assertEqual(self.bot.army_intent, 'defend')
        self.assertIn(64, (await self.bot.available_actions())[0])

    async def test_constrained_mode_keeps_its_existing_policy(self):
        self.bot.plan_mode = 'constrained'
        self.bot._set_army_intent('attack')
        self.set_time(101)
        # No plan: existing constrained fallback has no additional filter.
        choices, blocked = await self.bot.available_actions()
        self.assertEqual(choices, self.choices)
        self.assertFalse(blocked)
        self.assertFalse(self.bot._army_command_guard()['enabled'])

    async def test_snapshot_and_summary_expose_rule_separately_from_astra_filter(self):
        await self.execute(64)
        with patch.object(JevProtossBot, '_snapshot', return_value={'resource': {}}):
            state = self.bot._snapshot()
        self.assertFalse(state['strategy_status']['astra_action_filter_enabled'])
        self.assertTrue(state['army_command_guard']['enabled'])
        self.assertEqual(state['army_command_guard']['blocked_actions'], {'72': 'army_command_attack_hold'})
        json.dumps(state, allow_nan=False)
        payload = self.bot.scheduler.client.payload(state, {65: 'MULTI-RETREAT', 71: 'EMPTY ACTION'})
        instructions = payload['questions']['next_macro_action']['instructions']
        self.assertIn('retreat 65 remains available', instructions)
        self.assertIn('Production and EMPTY ACTION keep the current army mission running', instructions)
        await self.bot.available_actions()
        await self.bot.shutdown('test')
        self.assertGreater(self.bot._planner_summary['army_command_guard_blocks']['army_command_attack_hold'], 0)

    async def test_initial_guard_summary_needs_no_game_state(self):
        # Runner cleanup can happen after construction but before SC2 starts.
        self.bot.state = None
        self.assertEqual(self.bot._army_command_guard()['blocked_actions'], {})

    def test_nonfinite_negative_and_disabled_cli_values(self):
        for text in ('-1', 'nan', 'inf', '-inf'):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                nonnegative_float(text)
        self.assertEqual(nonnegative_float('0'), 0)
        self.assertEqual(nonnegative_float('20'), 20)

    async def test_nondefault_duration_is_respected(self):
        self.bot.advisory_posture_hold = 5
        await self.execute(64)
        self.set_time(104)
        self.assertNotIn(72, (await self.bot.available_actions())[0])
        self.set_time(105)
        self.assertIn(72, (await self.bot.available_actions())[0])


if __name__ == '__main__':
    unittest.main()
