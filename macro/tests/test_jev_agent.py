import asyncio
import json
import unittest

import httpx

from sc2_rl_agent.starcraftenv_test.agent.jev_agent import (
    DecisionScheduler, JevClient, JevError, QUESTION,
)


def reply(choice="0", probabilities=None):
    return {"model": "jev-1.13.0", "answers": {QUESTION: {
        "type": "choice", "choice": choice, "confidence": 0.8,
        "probabilities": probabilities or {"0": 0.8, "71": 0.2},
    }}, "usage": {"input_tokens": 100, "output_tokens": 20}}


class NativeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_contract_and_valid_choice(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.typesafe.ai/v1/systemone")
            self.assertEqual(request.headers["authorization"], "Bearer test-only")
            body = json.loads(request.content)
            self.assertNotIn("messages", body)
            self.assertEqual(body["state"], {"minerals": 50})
            self.assertEqual(body["questions"][QUESTION]["criteria"], {"0": "TRAIN PROBE", "71": "EMPTY ACTION"})
            return httpx.Response(200, json=reply())
        client = JevClient("test-only", transport=httpx.MockTransport(handler))
        try:
            answer = await client.choose(client.payload({"minerals": 50}, {0: "TRAIN PROBE", 71: "EMPTY ACTION"}))
            self.assertEqual(answer["answer"]["choice"], "0")
        finally:
            await client.close()

    async def test_reject_out_of_mask_choice_and_invalid_distribution(self):
        for body in [reply("64"), reply(probabilities={"0": 0.9, "71": 0.9}),
                     reply(probabilities={"0": float("nan"), "71": 0.2})]:
            client = JevClient("test-only", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=json.dumps(body))))
            try:
                with self.assertRaisesRegex(JevError, "invalid_response"):
                    await client.choose(client.payload({}, {0: "TRAIN PROBE", 71: "EMPTY ACTION"}))
            finally:
                await client.close()

    async def test_total_timeout(self):
        async def delayed(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=reply())
        client = JevClient("test-only", timeout=0.01, transport=httpx.MockTransport(delayed))
        try:
            with self.assertRaisesRegex(JevError, "request_timeout"):
                await client.choose(client.payload({}, {0: "TRAIN PROBE", 71: "EMPTY ACTION"}))
        finally:
            await client.close()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.release = asyncio.Event()
        self.requests = []
        self.events = []

        async def delayed(request):
            self.requests.append(json.loads(request.content))
            await self.release.wait()
            return httpx.Response(200, json=reply())

        self.client = JevClient("test-only", transport=httpx.MockTransport(delayed))
        self.scheduler = DecisionScheduler(self.client, lambda event, **kw: self.events.append((event, kw)),
                                           interval=1, max_age=4, max_requests=2, clock=lambda: self.now)
        self.choices = {0: "TRAIN PROBE", 71: "EMPTY ACTION"}

    async def asyncTearDown(self):
        await self.scheduler.close()

    async def complete(self):
        self.release.set()
        await self.scheduler.task

    async def test_game_progresses_while_pending_immutable_snapshot_and_once_only(self):
        state = {"economy": {"minerals": 50}}
        self.assertTrue(self.scheduler.submit(10, state, self.choices))
        state["economy"]["minerals"] = 500
        # Simulated game callbacks continue without waiting for HTTP completion.
        for game_loop in range(11, 25):
            self.assertIsNone(self.scheduler.poll(game_loop))
            self.assertFalse(self.scheduler.submit(game_loop, state, self.choices))
            await asyncio.sleep(0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["state"]["economy"]["minerals"], 50)
        self.now += 0.5
        await self.complete()
        decision = self.scheduler.poll(25)
        self.assertEqual(decision.action_id, 0)
        self.assertIsNone(self.scheduler.poll(25))
        self.assertEqual(self.scheduler.stats["responses"], 1)

    async def test_expired_game_state_is_discarded(self):
        self.scheduler.submit(10, {}, self.choices)
        await self.complete()
        self.assertIsNone(self.scheduler.poll(110))
        self.assertEqual(self.scheduler.stats["stale_results"], 1)

    async def test_expired_wall_time_is_discarded(self):
        self.scheduler.submit(10, {}, self.choices)
        await self.complete()
        self.now += 5
        self.assertIsNone(self.scheduler.poll(20))
        self.assertEqual(self.scheduler.stats["stale_results"], 1)

    async def test_rate_limit_backs_off_without_retrying_old_observation(self):
        await self.client.close()
        self.client = JevClient("test-only", transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "10"}, text="secret must not be logged")))
        self.scheduler.client = self.client
        self.scheduler.submit(10, {}, self.choices)
        with self.assertRaises(JevError):
            await self.scheduler.task
        self.assertIsNone(self.scheduler.poll(20))
        self.now += 9
        self.assertFalse(self.scheduler.ready)
        self.now += 2
        self.assertTrue(self.scheduler.ready)
        self.assertNotIn("secret", json.dumps(self.events))

    async def test_unauthorized_disables_further_calls(self):
        await self.client.close()
        self.client = JevClient("test-only", transport=httpx.MockTransport(
            lambda request: httpx.Response(401)))
        self.scheduler.client = self.client
        self.scheduler.submit(10, {}, self.choices)
        with self.assertRaises(JevError):
            await self.scheduler.task
        self.scheduler.poll(20)
        self.now += 100
        self.assertTrue(self.scheduler.disabled)
        self.assertFalse(self.scheduler.ready)

    async def test_shutdown_cancels_pending_request(self):
        self.scheduler.submit(10, {}, self.choices)
        task = self.scheduler.task
        await asyncio.sleep(0)
        await self.scheduler.close()
        self.assertTrue(task.cancelled())
        self.assertTrue(self.client.http.is_closed)
        self.assertFalse(self.scheduler.ready)

    async def test_payment_required_disables_retry_and_reports_bounded_billing_error(self):
        await self.client.close()
        self.client = JevClient("test-only", transport=httpx.MockTransport(
            lambda request: httpx.Response(402, json={'detail': 'private account data'})))
        self.scheduler.client = self.client
        self.scheduler.submit(10, {}, self.choices)
        with self.assertRaises(JevError):
            await self.scheduler.task
        self.assertIsNone(self.scheduler.poll(20))
        self.now += 1000
        self.assertFalse(self.scheduler.ready)
        with self.assertRaisesRegex(JevError, 'decision_service_unavailable:http_402'):
            self.scheduler.require_service()
        self.assertEqual(self.events[-1][1]['error_category'], 'billing')
        self.assertEqual(self.events[-1][1]['retry_in_seconds'], 0)
        self.assertNotIn('private account', json.dumps(self.events))

    async def test_transient_http_error_does_not_abort_trial(self):
        await self.client.close()
        self.client = JevClient("test-only", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.scheduler.client = self.client
        self.scheduler.submit(10, {}, self.choices)
        with self.assertRaises(JevError):
            await self.scheduler.task
        self.scheduler.poll(20)
        self.scheduler.require_service()
        self.now += 40
        self.assertTrue(self.scheduler.ready)


if __name__ == "__main__":
    unittest.main()
