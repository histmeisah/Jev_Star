"""An Astra recommendation must not turn into an implicit action restriction."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from test_jev_hierarchy import plan, catalog, resource
from sc2_rl_agent.starcraftenv_test.agent.jev_agent import (
    ADVISORY_INSTRUCTIONS, Decision, JevClient, QUESTION,
)
from sc2_rl_agent.starcraftenv_test.agent.astra_planner import (
    ADVISORY_PLANNER_INSTRUCTIONS, CodexPlannerClient, PLAN_SCHEMA,
)
from sc2_rl_agent.starcraftenv_test.env.bot.jev_protoss_bot import JevProtossBot
from sc2_rl_agent.starcraftenv_test.env.bot.hierarchical_protoss_bot import HierarchicalProtossBot


class AdvisoryPlanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = JevClient('test-only', transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        planner_client = SimpleNamespace(close=AsyncMock(), model='gpt-6-astra')
        self.bot = HierarchicalProtossBot(self.client, Path(self.temp.name), planner_client=planner_client,
                                         plan_mode='advisory')
        self.bot._initialize_variables()
        self.bot.state = SimpleNamespace(game_loop=2240)
        self.bot.planner.active = {**plan(), 'plan_id': 2, 'expires_game_seconds': 200,
                                   'army_posture': 'defend', 'army_target_id': 'home',
                                   'worker_target': 16, 'base_target': 1}
        self.bot._catalog = Mock(return_value=catalog())
        self.bot.get_information = Mock(return_value={'resource': {**resource(), 'ready_army_supply': 40}})
        self.bot._emergency = Mock(return_value=False)

    async def asyncTearDown(self):
        await self.bot.shutdown('test')
        self.temp.cleanup()

    async def test_all_base_choices_survive_defend_caps_and_budget_recommendations(self):
        choices = {0: 'TRAIN PROBE', 1: 'TRAIN ZEALOT', 21: 'BUILD NEXUS',
                   64: 'MULTI-ATTACK', 71: 'EMPTY ACTION'}
        blocked = {'27': 'building_resources'}
        self.bot._execution_directive = {'plan_id': 1, 'action_id': 72}
        with patch.object(JevProtossBot, 'available_actions', new=AsyncMock(return_value=(choices.copy(), blocked.copy()))):
            actual, reasons = await self.bot.available_actions()
        self.assertEqual(actual, choices)
        self.assertEqual(reasons, blocked)
        self.assertEqual(self.bot._base_available_actions, sorted(choices))
        self.assertFalse(self.bot._policy_blocks)
        self.assertIsNone(self.bot._execution_directive)

    async def test_default_constrained_mode_still_removes_attack_and_capped_workers(self):
        self.bot.plan_mode = 'constrained'
        c = catalog()
        c['21']['count_with_pending'] = 1
        self.bot._catalog.return_value = c
        choices = {0: 'TRAIN PROBE', 21: 'BUILD NEXUS', 64: 'MULTI-ATTACK', 71: 'EMPTY ACTION'}
        with patch.object(JevProtossBot, 'available_actions', new=AsyncMock(return_value=(choices, {}))):
            actual, reasons = await self.bot.available_actions()
        self.assertEqual(actual, {71: 'EMPTY ACTION'})
        self.assertEqual(reasons['64'], 'plan_attack_not_ready')
        self.assertEqual(reasons['0'], 'plan_worker_target_reached')

    async def test_new_recommendation_does_not_discard_a_still_executable_choice(self):
        self.bot.available_actions = AsyncMock(return_value=({0: 'TRAIN PROBE'}, {}))
        self.bot._train_one = AsyncMock()
        self.bot._track_production_order = Mock()
        await self.bot._execute_decision(Decision(1, 2236, 0, 0, {}, 100, plan_id=1))
        self.bot._train_one.assert_awaited_once()
        self.assertEqual(self.bot._action_stats['plan_changed_discards'], 0)

    async def test_live_base_rejection_still_prevents_execution(self):
        self.bot.available_actions = AsyncMock(return_value=({71: 'EMPTY ACTION'}, {'0': 'resources_or_supply'}))
        self.bot._train_one = AsyncMock()
        await self.bot._execute_decision(Decision(1, 2236, 0, 0, {}, 100, plan_id=1))
        self.bot._train_one.assert_not_awaited()
        self.assertEqual(self.bot._action_stats['invalidated_before_execution'], 1)

    async def test_plan_target_cannot_silently_control_navigation(self):
        self.bot.planner.active['army_target_id'] = 'expansion_8'
        self.assertIsNone(self.bot._planned_target())
        self.bot.plan_mode = 'constrained'
        self.assertEqual(self.bot._planned_target(), 'expansion_8')

    async def test_payload_contains_plan_and_actual_base_candidates_without_obedience_prompt(self):
        original_plan = copy.deepcopy(self.bot.planner.active)
        self.bot._base_available_actions = [0, 64, 71]
        with patch.object(JevProtossBot, '_snapshot', return_value={'resource': resource()}):
            state = self.bot._snapshot()
        payload = self.client.payload(state, {0: 'TRAIN PROBE', 64: 'MULTI-ATTACK', 71: 'EMPTY ACTION'})
        question = payload['questions'][QUESTION]
        self.assertTrue(question['instructions'].startswith(ADVISORY_INSTRUCTIONS))
        self.assertIn('fixed executor rule, independent of Astra', question['instructions'])
        self.assertIn('including actions outside that plan', question['instructions'])
        self.assertNotIn('Follow strategic_plan', question['instructions'])
        self.assertEqual(state['strategic_plan']['plan_id'], 2)
        self.assertEqual(state['strategy_status']['base_available_actions'], [0, 64, 71])
        self.assertFalse(state['strategy_status']['astra_action_filter_enabled'])
        self.assertIsNone(state['execution_directive'])
        self.assertEqual(self.bot.planner.active, original_plan)

    async def test_disabling_guard_preserves_original_advisory_prompt(self):
        self.bot.advisory_posture_hold = 0
        with patch.object(JevProtossBot, '_snapshot', return_value={'resource': resource()}):
            state = self.bot._snapshot()
        payload = self.client.payload(state, {64: 'MULTI-ATTACK', 71: 'EMPTY ACTION'})
        self.assertEqual(payload['questions'][QUESTION]['instructions'], ADVISORY_INSTRUCTIONS)
        self.assertFalse(state['army_command_guard']['enabled'])


class AdvisoryPlannerPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_advisory_prompt_is_sent_and_saved_with_original_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.find_codex', return_value=Path('fake.exe')):
                client = CodexPlannerClient(directory, plan_mode='advisory')
            sent = []
            def popen(command, **kwargs):
                output = Path(command[command.index('--output-last-message') + 1])
                process = Mock(returncode=0)
                def communicate(prompt, **_):
                    sent.append(prompt)
                    output.write_text(json.dumps(plan()), encoding='utf-8')
                    return json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10}}), ''
                process.communicate.side_effect = communicate
                return process
            payload = {'state': {'plan_mode': 'advisory'}}
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.subprocess.Popen', side_effect=popen):
                response = await client.plan(1, payload)
            saved = (Path(directory) / 'planner/request-0001.txt').read_text(encoding='utf-8')
            self.assertEqual(saved, sent[0])
            self.assertTrue(saved.startswith(ADVISORY_PLANNER_INSTRUCTIONS))
            schema_text = saved.split('REQUIRED JSON SCHEMA (same schema supplied via --output-schema):\n', 1)[1].split('\nINPUT JSON:\n', 1)[0]
            self.assertEqual(json.loads(schema_text), PLAN_SCHEMA)
            self.assertEqual(response['plan'], plan())
            await client.close()
