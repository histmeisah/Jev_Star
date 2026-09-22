"""SC2 startup must terminate promptly if its own client exits."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sc2_rl_agent.starcraftenv_test.utils import sc2_runtime as runtime


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_dead_client_cancels_connection_without_waiting_for_socket_timeout(self):
        process = object.__new__(runtime.WindowedSC2Process)
        process._process = Mock()
        process._process.poll.return_value = 1
        with patch.object(runtime.SC2Process, '_connect', new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, 'exited during startup'):
                await asyncio.wait_for(process._connect(), timeout=.2)

    async def test_successful_connection_is_returned(self):
        process = object.__new__(runtime.WindowedSC2Process)
        process._process = Mock()
        process._process.poll.return_value = None
        websocket = object()
        with patch.object(runtime.SC2Process, '_connect', new_callable=AsyncMock, return_value=websocket):
            self.assertIs(await process._connect(), websocket)


class FactoryTests(unittest.TestCase):
    def test_game_failure_restores_process_factory(self):
        original = runtime.sc2_main.SC2Process
        with patch.object(runtime.os, 'name', 'nt'):
            with patch.object(runtime.sc2_main, 'run_game', side_effect=RuntimeError('test launch failure')):
                with self.assertRaisesRegex(RuntimeError, 'test launch failure'):
                    runtime.run_windowed_game()
        self.assertIs(runtime.sc2_main.SC2Process, original)


if __name__ == '__main__':
    unittest.main()
