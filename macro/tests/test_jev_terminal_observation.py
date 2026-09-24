"""Terminal state must be returned before a requested future frame exists."""
import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from sc2.data import Result
from sc2_rl_agent.starcraftenv_test.utils import sc2_runtime


def observation(frame, ended=False):
    return SimpleNamespace(observation=SimpleNamespace(
        observation=SimpleNamespace(game_loop=frame), player_result=[1] if ended else []))


class RealtimeObservationTests(IsolatedAsyncioTestCase):
    async def test_end_before_requested_frame_returns_without_future_request(self):
        client = sc2_runtime.RealtimeObservationClient.__new__(sc2_runtime.RealtimeObservationClient)
        client._game_result = None
        ended = observation(101, True)
        with patch.object(sc2_runtime.Client, "observation", new=AsyncMock(side_effect=[observation(100), ended])) as base:
            result = await client.observation(104)
        self.assertIs(result, ended)
        self.assertEqual(base.await_count, 2)
        self.assertTrue(all(not c.args and not c.kwargs for c in base.await_args_list))

    async def test_live_game_waits_for_requested_frame(self):
        client = sc2_runtime.RealtimeObservationClient.__new__(sc2_runtime.RealtimeObservationClient)
        client._game_result = None
        ready = observation(104)
        with patch.object(sc2_runtime.Client, "observation", new=AsyncMock(side_effect=[observation(100), observation(102), ready])) as base:
            self.assertIs(await client.observation(104), ready)
        self.assertEqual(base.await_count, 3)

    async def test_unbounded_observation_does_not_wait(self):
        client = sc2_runtime.RealtimeObservationClient.__new__(sc2_runtime.RealtimeObservationClient)
        client._game_result = None
        ready = observation(100)
        with patch.object(sc2_runtime.Client, "observation", new=AsyncMock(return_value=ready)) as base:
            self.assertIs(await client.observation(), ready)
        self.assertEqual(base.await_count, 1)


class RuntimeFactoryTests(TestCase):
    def test_override_is_scoped_and_nonrealtime_uses_original_client(self):
        original = sc2_runtime.sc2_main.Client
        original_play = sc2_runtime.sc2_main._play_game_ai
        def running(*args, **kwargs):
            expected = sc2_runtime.RealtimeObservationClient if kwargs["realtime"] else original
            self.assertIs(sc2_runtime.sc2_main.Client, expected)
            raise RuntimeError("simulated launch failure")
        for realtime in (True, False):
            with patch.object(sc2_runtime.sc2_main, "run_game", side_effect=running):
                with self.assertRaises(RuntimeError):
                    sc2_runtime.run_windowed_game(realtime=realtime)
            self.assertIs(sc2_runtime.sc2_main.Client, original)
            self.assertIs(sc2_runtime.sc2_main._play_game_ai, original_play)

    def test_query_race_delivers_actual_result_once_before_host_cleanup(self):
        ai = SimpleNamespace(on_end=AsyncMock())
        client = SimpleNamespace(_game_result={1: Result.Defeat})
        async def ended(*args, **kwargs):
            raise sc2_runtime._RealtimeGameEnded()
        def run(*args, **kwargs):
            return asyncio.run(sc2_runtime.sc2_main._play_game_ai(client, 1, ai, True, None))
        with patch.object(sc2_runtime.sc2_main, '_play_game_ai', new=ended), \
             patch.object(sc2_runtime.sc2_main, 'run_game', side_effect=run):
            self.assertEqual(sc2_runtime.run_windowed_game(realtime=True), Result.Defeat)
            self.assertIs(sc2_runtime.sc2_main._play_game_ai, ended)
        ai.on_end.assert_awaited_once_with(Result.Defeat)


class TerminalRequestRaceTests(IsolatedAsyncioTestCase):
    async def test_game_ended_query_reads_result_and_unwinds_without_generic_failure(self):
        client = sc2_runtime.RealtimeObservationClient.__new__(sc2_runtime.RealtimeObservationClient)
        client._game_result = None
        async def terminal():
            client._game_result = {1: Result.Defeat}
        client.observation = AsyncMock(side_effect=terminal)
        error = sc2_runtime.ProtocolError("['Not supported if game has already ended']")
        with patch.object(sc2_runtime.Client, '_execute', new=AsyncMock(side_effect=error)):
            with self.assertRaises(sc2_runtime._RealtimeGameEnded):
                await client._execute(query=object())
        client.observation.assert_awaited_once_with()

    async def test_game_ended_error_without_result_is_not_invented_as_defeat(self):
        client = sc2_runtime.RealtimeObservationClient.__new__(sc2_runtime.RealtimeObservationClient)
        client._game_result = None
        client.observation = AsyncMock()
        error = sc2_runtime.ProtocolError("['Not supported if game has already ended']")
        with patch.object(sc2_runtime.Client, '_execute', new=AsyncMock(side_effect=error)):
            with self.assertRaises(sc2_runtime.ProtocolError) as caught:
                await client._execute(query=object())
        self.assertIs(caught.exception, error)

    async def test_unrelated_errors_are_preserved(self):
        client = sc2_runtime.RealtimeObservationClient.__new__(sc2_runtime.RealtimeObservationClient)
        client.observation = AsyncMock()
        error = sc2_runtime.ProtocolError("['Invalid request']")
        with patch.object(sc2_runtime.Client, '_execute', new=AsyncMock(side_effect=error)):
            with self.assertRaises(sc2_runtime.ProtocolError) as caught:
                await client._execute(query=object())
        self.assertIs(caught.exception, error)
        client.observation.assert_not_awaited()
