"""Local Windows startup compatibility for BurnySC2's legacy run_game entry."""

import asyncio
import os
from contextlib import suppress
from functools import partial

from sc2 import main as sc2_main
from sc2.sc2process import SC2Process
from sc2.client import Client
from sc2.protocol import ProtocolError


class _RealtimeGameEnded(BaseException):
    """Terminal control flow bypasses bot handlers for ordinary step failures."""


class RealtimeObservationClient(Client):
    """Poll present observations so a future-frame request cannot hide game end."""

    async def _execute(self, **kwargs):
        try:
            return await super()._execute(**kwargs)
        except ProtocolError as exc:
            # A realtime game can end between observation and an ability query
            # or action. Obtain the actual outcome before leaving the bot loop.
            gameplay_request = bool(set(kwargs) & {"query", "action", "game_info", "data", "step"})
            if not gameplay_request or not exc.is_game_over_error:
                raise
            await self.observation()
            if not self._game_result:
                raise
            raise _RealtimeGameEnded() from exc

    async def observation(self, game_loop=None):
        while True:
            result = await super().observation()
            if (game_loop is None or self._game_result
                    or result.observation.player_result
                    or result.observation.observation.game_loop >= game_loop):
                return result
            # Preserve the SDK's requested callback frame, while checking terminal
            # results even if SC2 has stopped advancing before that frame.
            await asyncio.sleep(0.02)


class WindowedSC2Process(SC2Process):
    def __init__(self, *args, **kwargs):
        self._event_sink = kwargs.pop("event_sink", None)
        kwargs.setdefault("resolution", (1280, 720))
        kwargs.setdefault("placement", (50, 50))
        super().__init__(*args, **kwargs)

    async def _connect(self):
        emit = getattr(self, "_event_sink", None)
        if emit:
            emit("sc2_process_started", pid=self._process.pid, host=self._host,
                 port=self._port, display_arguments=self._arguments)
        connection = asyncio.create_task(super()._connect())
        try:
            for _ in range(60):
                if self._process.poll() is not None:
                    if emit:
                        emit("sc2_startup_error", pid=self._process.pid, exit_code=self._process.returncode,
                             error="process_exited_before_api_connection")
                    raise RuntimeError("SC2 exited during startup; inspect its Graphics log.")
                done, _ = await asyncio.wait({connection}, timeout=1)
                if done:
                    result = connection.result()
                    if emit:
                        emit("sc2_connected", pid=self._process.pid)
                    return result
            raise TimeoutError("SC2 startup exceeded 60 seconds")
        finally:
            if not connection.done():
                connection.cancel()
                with suppress(asyncio.CancelledError):
                    await connection


def run_windowed_game(*args, **kwargs):
    # run_game in SDK 6.5 has no sc2_config parameter. Limit its process factory
    # override to this invocation; no installed SDK or other process is modified.
    previous = sc2_main.SC2Process
    previous_client = sc2_main.Client
    previous_play_ai = sc2_main._play_game_ai
    event_sink = kwargs.pop("event_sink", None)
    if os.name == "nt":
        sc2_main.SC2Process = partial(WindowedSC2Process, event_sink=event_sink)
    if kwargs.get("realtime", False):
        sc2_main.Client = RealtimeObservationClient

        async def play_ai(client, player_id, ai, *play_args, **play_kwargs):
            try:
                return await previous_play_ai(client, player_id, ai, *play_args, **play_kwargs)
            except _RealtimeGameEnded:
                result = client._game_result[player_id]
                await ai.on_end(result)
                return result

        sc2_main._play_game_ai = play_ai
    try:
        return sc2_main.run_game(*args, **kwargs)
    finally:
        sc2_main.SC2Process = previous
        sc2_main.Client = previous_client
        sc2_main._play_game_ai = previous_play_ai
