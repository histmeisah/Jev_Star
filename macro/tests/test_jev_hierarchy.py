"""Strategic budgets, Codex process lifecycle, and two-model callback regressions."""

import asyncio
import contextlib
import copy
import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from sc2.data import Race
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.units import Units

import test_jev_protoss as support
from sc2_rl_agent.starcraftenv_test.agent.astra_planner import (
    CodexPlannerClient, PlannerError, StrategicPlanner, validate_plan)
from sc2_rl_agent.starcraftenv_test.agent.jev_agent import Decision, JevClient, QUESTION
from sc2_rl_agent.starcraftenv_test.agent.strategic_policy import plan_progress, policy_reason
from sc2_rl_agent.starcraftenv_test.env.bot.hierarchical_protoss_bot import HierarchicalProtossBot


def plan():
    return {"objective": "Expand safely", "goals": [{"action_id": 21, "target": 2}],
            "worker_target": 22, "base_target": 2, "allowed_spending_actions": [0, 1, 19, 21],
            "reserve_for_action": 21, "reserve_after_workers": 16, "army_posture": "attack",
            "attack_min_army": 30, "retreat_below_army": 15, "min_posture_seconds": 20,
            "guidance": "Save for the second Nexus; defend if necessary.",
            "priority_action": None, "production_priority": [], "army_target_id": None}


def catalog():
    c = {str(a): {"cost": {"minerals": 0, "gas": 0}, "count_with_pending": 0} for a in range(72)}
    for action, cost in [(0, 50), (1, 100), (19, 100), (21, 400)]:
        c[str(action)]["cost"]["minerals"] = cost
    c["0"]["count_with_pending"] = 16
    c["21"]["count_with_pending"] = 1
    return c


def resource():
    return {"worker_supply": 16, "mineral": 350, "gas": 0, "supply_left": 5,
            "supply_cap": 31, "army_supply": 40}


class PlanPolicyTests(unittest.TestCase):
    def test_reserve_blocks_small_purchases_but_not_nexus_or_free_actions(self):
        args = (plan(), catalog(), resource(), "defend", 0, 100, False)
        self.assertEqual(policy_reason(0, *args), "plan_resource_reservation")
        self.assertEqual(policy_reason(1, *args), "plan_resource_reservation")
        self.assertIsNone(policy_reason(21, *args))
        self.assertIsNone(policy_reason(71, *args))

    def test_pending_goal_releases_reserve_without_double_expansion(self):
        c = catalog()
        c["21"]["count_with_pending"] = 2
        self.assertEqual(plan_progress(plan(), c, resource())["reserved_minerals"], 0)
        self.assertIsNone(policy_reason(1, plan(), c, resource(), "defend", 0, 100, False))
        self.assertEqual(policy_reason(21, plan(), c, resource(), "defend", 0, 100, False),
                         "plan_base_target_reached")

    def test_urgent_supply_and_defense_can_spend_reserve(self):
        r = resource()
        r["supply_left"] = 0
        self.assertIsNone(policy_reason(19, plan(), catalog(), r, "defend", 0, 100, False))
        self.assertIsNone(policy_reason(1, plan(), catalog(), r, "defend", 0, 100, True))
        c = catalog()
        c["0"]["count_with_pending"] = 22
        self.assertEqual(policy_reason(0, plan(), c, r, "defend", 0, 100, True), "plan_worker_target_reached")

    def test_posture_holds_but_allows_emergency_retreat(self):
        self.assertEqual(policy_reason(65, plan(), catalog(), resource(), "attack", 99, 100, False),
                         "plan_hold_army_intent")
        self.assertIsNone(policy_reason(65, plan(), catalog(), resource(), "attack", 99, 100, True))
        self.assertEqual(policy_reason(65, plan(), catalog(), resource(), "attack", 0, 100, False),
                         "plan_continue_attack")

    def test_semantic_validation_rejects_bad_reservations_and_caps(self):
        self.assertEqual(validate_plan(plan()), plan())
        for key, value in [("reserve_for_action", 64), ("worker_target", 200),
                           ("retreat_below_army", 40), ("reserve_after_workers", 23)]:
            with self.subTest(key=key):
                p = plan()
                p[key] = value
                with self.assertRaises(PlannerError): validate_plan(p)
        p = plan()
        p["goals"] = [{"action_id": 0, "target": 23}]
        with self.assertRaises(PlannerError): validate_plan(p)


class PlannerSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_refresh_and_poll_without_waiting_for_another_strategy_tick(self):
        now, events, payloads = [0.0], [], []
        async def choose(rid, payload):
            payloads.append(payload)
            return {"plan": plan(), "usage": {}, "model": "gpt-6-astra"}
        client = SimpleNamespace(plan=choose, close=AsyncMock(), model="gpt-6-astra")
        s = StrategicPlanner(client, lambda e, **kw: events.append((e, kw)), clock=lambda: now[0])
        s.tick(0, {}, catalog())
        await s.task
        now[0] = .2
        s.poll(4)  # The 1-second observation sampling period has not elapsed.
        self.assertEqual(s.active["plan_id"], 1)
        self.assertEqual(payloads[0]["triggers"], ["opening"])
        now[0] = 29
        s.tick(649, {}, catalog())
        self.assertEqual(s.request_id, 1)
        now[0] = 60
        s.tick(1344, {}, catalog())
        await s.task
        s.poll(1348)
        self.assertEqual(s.active["plan_id"], 2)
        self.assertEqual(payloads[1]["triggers"], ["periodic"])
        await s.close()

    async def test_events_coalesce_while_inflight_then_refresh_using_new_observation(self):
        now, release, payloads = [0.0], asyncio.Event(), []
        async def choose(rid, payload):
            payloads.append(payload)
            await release.wait()
            return {"plan": plan(), "usage": {}, "model": "gpt-6-astra"}
        s = StrategicPlanner(SimpleNamespace(plan=choose, close=AsyncMock(), model="gpt-6-astra"),
                             Mock(), clock=lambda: now[0])
        s.tick(0, {"mineral": 50}, catalog())
        await asyncio.sleep(0)
        self.assertTrue(s.trigger("new_enemy_composition", types=["ROACH"]))
        self.assertFalse(s.trigger("new_enemy_composition", types=["ROACH", "HYDRALISK"]))
        now[0] = 5
        s.tick(112, {"mineral": 100}, catalog())
        self.assertEqual(len(payloads), 1)
        release.set()
        await s.task
        now[0] = 12
        s.tick(268, {"mineral": 200}, catalog())
        self.assertIsNone(s.task)  # A normal sighting waits for the new plan to execute.
        self.assertEqual(s.request_id, 1)
        now[0] = 42
        s.tick(941, {"mineral": 300}, catalog())
        await s.task
        self.assertEqual(payloads[1]["state"]["mineral"], 300)
        self.assertEqual(payloads[1]["triggers"], ["new_enemy_composition"])
        self.assertEqual(payloads[1]["trigger_details"]["new_enemy_composition"]["types"], ["ROACH", "HYDRALISK"])
        self.assertTrue(s.trigger("new_enemy_composition"))  # The original 30-second cooldown elapsed.
        self.assertFalse(s.trigger("new_enemy_composition"))
        await s.close()

    async def test_urgent_event_invalidates_pending_plan_but_accounts_its_usage(self):
        now, events = [0.0], []
        client = SimpleNamespace(plan=AsyncMock(return_value={"plan": plan(), "usage": {"input_tokens": 100},
                                                             "model": "gpt-6-astra"}),
                                 close=AsyncMock(), model="gpt-6-astra")
        s = StrategicPlanner(client, lambda e, **kw: events.append((e, kw)), clock=lambda: now[0])
        s.tick(0, {}, catalog())
        await s.task
        s.trigger("base_attacked")
        now[0] = 12
        s.poll(268)
        self.assertIsNone(s.active)
        self.assertEqual(s.stats["superseded"], 1)
        self.assertEqual(s.stats["input_tokens"], 100)
        self.assertEqual(events[-1][1]["reason"], "battlefield_changed_during_planning")
        s.tick(268, {"under_attack": True}, catalog())
        await s.task
        s.poll(272)
        self.assertEqual(s.active["plan_id"], 2)
        self.assertEqual(s.stats["accepted"], 1)
        await s.close()

    async def test_nonblocking_snapshot_once_only_and_expiration(self):
        release = asyncio.Event()
        received, events = [], []
        clock = [0.0]
        async def choose(request_id, payload):
            received.append(payload)
            await release.wait()
            return {"plan": plan(), "usage": {"input_tokens": 20}, "model": "gpt-6-astra"}
        client = SimpleNamespace(plan=choose, close=AsyncMock(), model="gpt-6-astra")
        scheduler = StrategicPlanner(client, lambda event, **kw: events.append((event, kw)),
                                     ttl=10, max_requests=1, clock=lambda: clock[0])
        state = {"mineral": 350}
        scheduler.tick(0, state, catalog())
        state["mineral"] = 0
        await asyncio.sleep(0)
        for loop in range(4, 40, 4): scheduler.tick(loop, state, catalog())
        self.assertEqual(scheduler.stats["requests"], 1)
        self.assertEqual(received[0]["state"]["mineral"], 350)
        release.set()
        await scheduler.task
        clock[0] = 1
        scheduler.tick(40, state, catalog())
        scheduler.tick(44, state, catalog())
        self.assertEqual(scheduler.stats["accepted"], 1)
        scheduler.tick(400, state, catalog())
        self.assertIsNone(scheduler.active)
        self.assertEqual(scheduler.stats["expired"], 1)
        await scheduler.close()

    async def test_stale_plan_rejected_and_errors_leave_current_plan(self):
        clock = [0.0]
        client = SimpleNamespace(plan=AsyncMock(return_value={"plan": plan(), "usage": {}, "model": "gpt-6-astra"}),
                                 close=AsyncMock(), model="gpt-6-astra")
        s = StrategicPlanner(client, Mock(), interval=30, max_age=5, max_requests=2, clock=lambda: clock[0])
        s.tick(0, {}, catalog())
        await s.task
        clock[0] = 6
        s.tick(4, {}, catalog())
        self.assertIsNone(s.active)
        self.assertEqual(s.stats["stale"], 1)
        client.plan.side_effect = PlannerError("codex_timeout")
        s.active = {"plan_id": 7, "expires_game_seconds": 1000, **plan()}
        clock[0] = 40
        s.tick(900, {}, catalog())
        await asyncio.sleep(0)
        s.tick(904, {}, catalog())
        self.assertEqual(s.stats["errors"], 1)
        self.assertEqual(s.active["plan_id"], 7)
        await s.close()

    async def test_shutdown_cancels_background_task(self):
        pending = asyncio.Event()
        client = SimpleNamespace(plan=AsyncMock(side_effect=lambda *_: None), close=AsyncMock(), model="gpt-6-astra")
        async def wait(*_): await pending.wait()
        client.plan = wait
        s = StrategicPlanner(client, Mock())
        s.tick(0, {}, catalog())
        await asyncio.sleep(0)
        task = s.task
        await s.close()
        self.assertTrue(task.cancelled())
        client.close.assert_awaited_once()

    async def test_new_plan_clears_old_goal_event_and_urgent_threat_bypasses_window(self):
        now, events = [0.0], []
        client = SimpleNamespace(plan=AsyncMock(return_value={"plan": plan(), "usage": {}, "model": "astra"}),
                                 close=AsyncMock(), model="astra")
        s = StrategicPlanner(client, lambda e, **kw: events.append((e, kw)), clock=lambda: now[0])
        s.tick(0, {}, catalog())
        s.trigger("goal_completed", plan_id=0)
        await s.task
        now[0] = 15
        s.poll(336)
        self.assertNotIn("goal_completed", s.dirty)
        self.assertEqual(s.active["goal_counts_at_observation"], {"21": 1})
        self.assertEqual(s.active["expires_game_seconds"], 195)
        s.trigger("base_count_changed", current=2)
        s.tick(448, {}, catalog())
        self.assertIsNone(s.task)
        s.trigger("new_enemy_threat", types=["MUTALISK"])
        now[0] = 20
        s.tick(448, {}, catalog())
        self.assertIsNotNone(s.task)
        await s.task
        self.assertIn("new_enemy_threat", client.plan.call_args.args[1]["triggers"])
        await s.close()


class CadenceProgressTests(unittest.TestCase):
    def bot(self, p):
        return SimpleNamespace(planner=SimpleNamespace(active=p, trigger=Mock(), stats={}),
                               _last_plan_id=None, _completed_goals=set(), log=Mock(),
                               state=SimpleNamespace(game_loop=2240))

    def test_goals_already_satisfied_in_request_do_not_notify_on_acceptance(self):
        p = {"plan_id": 2, **plan(), "goal_counts_at_observation": {"21": 2}}
        bot, c = self.bot(p), catalog()
        c["21"]["count_with_pending"] = 2
        HierarchicalProtossBot._check_plan_progress(bot, {"resource": resource()}, c)
        bot.planner.trigger.assert_not_called()
        bot.log.assert_not_called()

    def test_worker_progress_is_logged_but_only_phase_completion_refreshes(self):
        p = {"plan_id": 2, **plan(), "goal_counts_at_observation": {"0": 16, "21": 1}}
        p["goals"].append({"action_id": 0, "target": 22})
        bot, c = self.bot(p), catalog()
        c["0"]["count_with_pending"] = 22
        HierarchicalProtossBot._check_plan_progress(bot, {"resource": resource()}, c)
        bot.planner.trigger.assert_not_called()
        self.assertFalse(bot.log.call_args.kwargs["requests_refresh"])
        c["21"]["count_with_pending"] = 2
        HierarchicalProtossBot._check_plan_progress(bot, {"resource": resource()}, c)
        bot.planner.trigger.assert_called_once()
        self.assertTrue(bot.planner.trigger.call_args.kwargs["phase_complete"])
        HierarchicalProtossBot._check_plan_progress(bot, {"resource": resource()}, c)
        bot.planner.trigger.assert_called_once()

    def test_identical_failure_is_deduplicated_until_success_or_cooldown(self):
        from collections import Counter
        bot = SimpleNamespace(empty_action=71, time=100, _failure_notifications={},
                              planner=SimpleNamespace(trigger=Mock(), stats=Counter()))
        outcome = {"action_id": 33, "orders_submitted": 0, "failures": ["No Forge"]}
        HierarchicalProtossBot._notify_execution_outcome(bot, outcome)
        bot.time = 160
        HierarchicalProtossBot._notify_execution_outcome(bot, outcome)
        bot.planner.trigger.assert_called_once()
        self.assertEqual(bot.planner.stats["duplicate_failures_suppressed"], 1)
        HierarchicalProtossBot._notify_execution_outcome(bot, {**outcome, "orders_submitted": 1})
        HierarchicalProtossBot._notify_execution_outcome(bot, outcome)
        self.assertEqual(bot.planner.trigger.call_count, 2)


class CodexClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_real_shape_preserves_usage_and_feedback_then_recovers(self):
        # The first live opening plans included Chronoboost in the spending list.
        # Reject that contract violation, but retain billed usage and the old plan.
        with tempfile.TemporaryDirectory() as directory:
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.find_codex', return_value=Path('fake.exe')):
                client = CodexPlannerClient(Path(directory))
            bad = plan()
            bad['allowed_spending_actions'].append(66)
            returned, prompts, events, now = [bad, plan()], [], [], [0.0]
            def fake_popen(args, **kwargs):
                result = Path(args[args.index('--output-last-message') + 1])
                process = Mock(returncode=0)
                def communicate(prompt, **_):
                    prompts.append(json.loads(prompt.split('INPUT JSON:\n', 1)[1]))
                    result.write_text(json.dumps(returned.pop(0)), encoding='utf-8')
                    return json.dumps({'type': 'turn.completed', 'usage': {
                        'input_tokens': 100, 'output_tokens': 20}}), ''
                process.communicate.side_effect = communicate
                return process
            scheduler = StrategicPlanner(client, lambda e, **kw: events.append({'event': e, **kw}),
                                         clock=lambda: now[0])
            scheduler.active = {'plan_id': 99, 'expires_game_seconds': 100, **plan()}
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.subprocess.Popen', side_effect=fake_popen):
                scheduler.tick(0, {}, catalog())
                await scheduler.task
                now[0] = 1
                scheduler.poll(22)
                self.assertEqual(scheduler.active['plan_id'], 99)
                self.assertEqual(scheduler.stats['input_tokens'], 100)
                self.assertEqual(scheduler.stats['invalid_plans'], 1)
                self.assertEqual(events[-1]['event'], 'planner_error')
                now[0] = 12
                scheduler.tick(268, {}, catalog())
                await scheduler.task
                scheduler.poll(272)
            self.assertEqual(prompts[1]['last_plan_rejection'], 'invalid_spending_action_id')
            self.assertIn('invalid_plan', prompts[1]['triggers'])
            self.assertEqual(scheduler.active['plan_id'], 2)
            self.assertIsNone(scheduler.last_rejection)
            self.assertEqual(scheduler.stats['input_tokens'], 200)
            self.assertEqual(sum(e['event'] == 'plan_accepted' for e in events), 1)
            from sc2_rl_agent.starcraftenv_test.utils.run_logging import RunMetrics
            metrics = RunMetrics()
            for event in events:
                metrics.observe(event)
            astra = metrics.snapshot()['models']['astra']
            self.assertEqual((astra['responses'], astra['errors'], astra['input_tokens']), (2, 1, 200))
            await scheduler.close()

    async def test_structured_result_parsing_in_background_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.find_codex', return_value=Path('fake.exe')):
                client = CodexPlannerClient(Path(directory))
            ticks = 0
            def fake_popen(args, **kwargs):
                result = Path(args[args.index('--output-last-message') + 1])
                p = Mock(returncode=0)
                def communicate(*_, **__):
                    time.sleep(.04)
                    result.write_text(json.dumps(plan()), encoding='utf-8')
                    return json.dumps({'type':'turn.completed','usage':{'input_tokens':10}}), ''
                p.communicate.side_effect = communicate
                return p
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.subprocess.Popen', side_effect=fake_popen):
                task = asyncio.create_task(client.plan(1, {'state': {}}))
                while not task.done():
                    ticks += 1
                    await asyncio.sleep(.005)
                result = await task
            self.assertGreater(ticks, 2)
            self.assertEqual(result['plan'], plan())
            self.assertEqual(result['usage']['input_tokens'], 10)
            self.assertIn('respect_system_proxy', client._command(Path(directory)/'out.json'))
            await client.close()

    async def test_timeout_kills_only_the_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.find_codex', return_value=Path('fake.exe')):
                client = CodexPlannerClient(Path(directory), timeout=.01)
            process = Mock()
            process.communicate.side_effect = [subprocess.TimeoutExpired('codex', .01), ('', '')]
            with patch('sc2_rl_agent.starcraftenv_test.agent.astra_planner.subprocess.Popen', return_value=process):
                with self.assertRaisesRegex(PlannerError, 'codex_timeout'):
                    await client.plan(1, {})
            process.kill.assert_called_once()
            self.assertIsNone(client._process)
            await client.close()


class HybridCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_plan_choice_is_discarded_even_when_the_action_remains_legal(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = SimpleNamespace(close=AsyncMock(), model="gpt-6-astra")
            client = JevClient("test-only", transport=httpx.MockTransport(lambda _: httpx.Response(500)))
            bot = HierarchicalProtossBot(client, Path(directory), planner_client=planner)
            bot._initialize_variables()
            bot.state = SimpleNamespace(game_loop=100)
            bot.planner.active = {"plan_id": 2, "expires_game_seconds": 100, **plan()}
            bot.available_actions = AsyncMock(return_value=({0: "TRAIN PROBE"}, {}))
            bot._train_one = AsyncMock()
            try:
                await bot._execute_decision(Decision(1, 96, 0, 0, {}, 100, plan_id=1))
                bot._train_one.assert_not_awaited()
                self.assertEqual(bot._action_stats["plan_changed_discards"], 1)
                await bot._execute_decision(Decision(2, 100, 0, 0, {}, 100, plan_id=2))
                bot._train_one.assert_awaited_once()
            finally:
                await bot.shutdown("test")
            events = [json.loads(line) for line in (Path(directory) / "events.jsonl").read_text().splitlines()]
            discarded = next(e for e in events if e["event"] == "action_discarded")
            self.assertEqual(discarded["reason"], "plan_changed_during_inference")
            self.assertEqual(discarded["execution_plan_id"], 2)

    async def test_catalog_counts_pending_once_and_exposes_technology_before_affordability(self):
        with tempfile.TemporaryDirectory() as directory:
            planner = SimpleNamespace(close=AsyncMock(), model='gpt-6-astra')
            client = JevClient('test-only', transport=httpx.MockTransport(lambda _: httpx.Response(500)))
            bot = HierarchicalProtossBot(client, Path(directory), planner_client=planner)
            bot._initialize_variables()
            natural = support.FakeUnit(2, U.NEXUS)
            natural.is_ready = False
            bot.structures = Units([support.FakeUnit(1, U.NEXUS), natural,
                                    support.FakeUnit(3, U.GATEWAY), support.FakeUnit(4, U.WARPGATE)], bot)
            bot.game_data = SimpleNamespace(upgrades={})
            bot.calculate_cost = Mock(return_value=SimpleNamespace(minerals=100, vespene=0))
            bot.already_pending = Mock(side_effect=lambda kind: int(kind == U.NEXUS))
            bot.can_afford = Mock(return_value=False)
            try:
                c = bot._catalog()
                json.dumps(c, allow_nan=False)
                self.assertEqual(len(c), 73)
                self.assertEqual(c['21']['count_with_pending'], 2)
                self.assertEqual(c['22']['count_with_pending'], 2)
                self.assertTrue(any(p.get('required_building') == 'CYBERNETICSCORE'
                                    for p in c['3']['production_alternatives']))
                self.assertFalse(c['59']['exists_in_game_version'])
                bot.can_afford.assert_not_called()
            finally:
                await bot.shutdown('test')

    async def test_both_models_inflight_game_advances_and_new_plan_revalidates_old_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            release_jev, release_astra = asyncio.Event(), asyncio.Event()
            async def jev_response(request):
                await release_jev.wait()
                return httpx.Response(200,json={'model':'jev-1.13.0','answers':{QUESTION:{
                    'type':'choice','choice':'0','confidence':.9,'probabilities':{'0':.9,'71':.1}}},'usage':{}})
            async def astra_response(*_):
                await release_astra.wait()
                p = plan()
                p.update(worker_target=0, reserve_after_workers=0)
                return {'plan':p,'usage':{},'model':'gpt-6-astra'}
            planner = SimpleNamespace(plan=astra_response, close=AsyncMock(), model='gpt-6-astra')
            client = JevClient('test-only',transport=httpx.MockTransport(jev_response))
            bot = HierarchicalProtossBot(client, Path(directory), planner_client=planner)
            bot._initialize_variables()
            bot.state = SimpleNamespace(game_loop=0)
            bot.enemy_race = Race.Zerg
            bot.game_info = SimpleNamespace(player_start_location=Point2((10,10)), start_locations=[Point2((100,100))], map_center=Point2((55,55)))
            bot.game_data = SimpleNamespace(abilities={}, upgrades={})
            probe, nexus = support.FakeUnit(1,U.PROBE), support.FakeUnit(2,U.NEXUS)
            bot.units = bot.workers = Units([probe],bot)
            bot.structures = bot.townhalls = support.FakeBases([nexus],bot)
            bot.already_pending = Mock(return_value=0)
            bot.already_pending_upgrade = Mock(return_value=0)
            bot.can_afford = Mock(side_effect=lambda item: item==U.PROBE)
            ability = TRAIN_INFO[U.NEXUS][U.PROBE]['ability']
            bot.get_available_abilities = AsyncMock(side_effect=lambda units, **kwargs:[[ability] if u.type_id==U.NEXUS else [] for u in units])
            bot._maintain_local_behaviors = AsyncMock()
            bot._catalog = Mock(side_effect=catalog)
            bot._strategy_snapshot = Mock(side_effect=lambda: {
                'resource':bot.get_information()['resource'], 'enemy_last_seen_120s':{},
                'strategic_metrics':{'base_under_attack':False,'base_resources':[]}})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    for i in range(10):
                        bot.state.game_loop = i*4
                        await bot.on_step(i)
                        await asyncio.sleep(0)
                    self.assertEqual(bot._steps,10)
                    self.assertGreater(bot._steps_while_inflight,0)
                    self.assertGreater(bot._planner_steps_inflight,0)
                    release_astra.set()
                    await bot.planner.task
                    release_jev.set()
                    await bot.scheduler.task
                    bot.state.game_loop = 48
                    await bot.on_step(12)
                self.assertEqual(bot.planner.stats['accepted'],1)
                self.assertEqual(bot._action_stats['invalidated_before_execution'],1)
                nexus.train.assert_not_called()
            finally:
                await bot.shutdown('test')


if __name__ == '__main__':
    unittest.main()
