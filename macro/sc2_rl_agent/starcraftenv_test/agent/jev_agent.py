"""Native Jev choice API and a single-flight, non-blocking decision scheduler."""

import asyncio
import copy
import json
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
QUESTION = "next_macro_action"
INSTRUCTIONS = (
    "You execute immediate Protoss macro decisions in real-time StarCraft II. Choose ONE "
    "available action that advances the current mission. Priorities: imminent survival "
    "and supply/power recovery; applying an executable army order; critical production; "
    "economy and optional upgrades. execution_directive identifies the command currently "
    "needing acknowledgement. Choose it promptly when its conditions remain safe. "
    "A legal attack after the commander's army threshold is reached should begin NOW: "
    "do not wait for all remaining production goals or upgrades. Produce replacements "
    "while the attack continues. Action 72 restores base defense after retreat. "
    "Each train/build choice starts at most one unit/building. Workers are assigned "
    "to resources automatically; no build order or army production runs automatically. "
    "Attack/retreat/defend sets persistent army intent. Current enemy observations are "
    "partial: missing enemies are unknown, not absent. Research values mean 0=not "
    "started, between 0 and 1=in progress, 1=completed. Consider pending production "
    "and recent outcomes to avoid duplicate or repeatedly rejected actions. Prefer "
    "the earliest attainable production_priority, preserving a critical unit's reserve. "
    "EMPTY ACTION is justified only by a concrete resource/production wait or no useful "
    "legal action. Unfinished optional goals do not justify waiting instead of a ready "
    "army order. Return only a choice from the supplied criteria."
)

ADVISORY_INSTRUCTIONS = (
    "You choose immediate Protoss macro actions in real-time StarCraft II. Select ONE "
    "action from the supplied criteria using the current observation. Astra's "
    "strategic_plan is advisory context: its objective, production goals, worker/base "
    "targets, spending list, resource reserves, priorities, army posture and attack "
    "thresholds are suggestions, not permissions, ceilings or prerequisites. You may "
    "choose any supplied action, including actions outside that plan, when current "
    "conditions justify it. Judge survival, supply/power, sustainable income, combat "
    "production and opportunities to attack from the observation. Do not wait solely "
    "because a suggested goal or army threshold is unfinished. Reconsider outdated "
    "advice when enemies, resources or available units change. Each train/build "
    "choice starts at most one unit/building; no automatic production or build order "
    "exists. Workers are assigned automatically. Attack/retreat/defend sets persistent "
    "army intent; destinations use the ordinary executor's observed-enemy/search "
    "navigation, not the plan's army_target_id. Action 72 restores base defense "
    "after retreat. Scouts continue searching after assignment, and one Observer "
    "escorts the army. Missing enemies are unknown, not absent. Research values "
    "mean 0=not started, between 0 and 1=in progress, 1=completed. Account for "
    "pending production and recent execution feedback. Use EMPTY ACTION for a "
    "concrete resource/production wait or when no useful action is available. "
    "Return only a choice from the supplied criteria."
)


def load_api_key(config_file: Optional[Path] = None) -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and config_file is not None and config_file.is_file():
        match = re.search(r"(?m)^api\s*:\s*(\S+)\s*$", config_file.read_text(encoding="utf-8-sig"))
        if match:
            key = match.group(1)
    if not key:
        raise ValueError("Set TYPESAFE_API_KEY or provide a config file containing an api: entry.")
    return key


class JevError(Exception):
    """Sanitized error: never include the request headers, credentials or response body."""

    def __init__(self, message: str, status: Optional[int] = None, retry_after: float = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class JevClient:
    def __init__(self, api_key: str, model="jev-1.13.0", timeout=2.5, transport=None):
        self.model = model
        self.timeout = timeout
        self.http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def payload(self, state: dict, choices: dict) -> dict:
        if not 1 <= len(choices) <= 255:
            raise ValueError("Jev requires between 1 and 255 choices.")
        advisory = state.get("strategy_status", {}).get("plan_mode") == "advisory"
        instructions = ADVISORY_INSTRUCTIONS if advisory else INSTRUCTIONS
        if advisory and state.get("army_command_guard", {}).get("enabled"):
            instructions += (
                " army_command_guard is a fixed executor rule, independent of Astra: "
                "after starting an attack, ordinary defense 72 is briefly unavailable; "
                "retreat 65 remains available when withdrawal is needed. After leaving "
                "an attack, reattack 64 waits for the recorded cooldown. A continuing "
                "base alarm alone does not show that danger increased. Production "
                "and EMPTY ACTION keep the current army mission running."
            )
        if state.get("strategic_plan") and not advisory:
            instructions += (
                " Follow strategic_plan and execution_directive. Goals are total production "
                "ceilings for replenishment, not prerequisites for army movement. Respect "
                "worker/base targets and the effective spending reservation. Prioritize the "
                "reserved goal when affordable. Ignore a suspended reservation until its "
                "reported blocker clears. Urgent supply and defense may override the budget. "
                "Scouts search expansions automatically after assignment, and one Observer "
                "escorts the army; repeated scouting is not necessary every decision."
            )
        return {
            "model": self.model,
            "state": state,
            "questions": {QUESTION: {
                "type": "choice", "instructions": instructions,
                "criteria": {str(k): v for k, v in choices.items()},
            }},
        }

    async def choose(self, payload: dict) -> dict:
        try:
            # HTTPX's timeout is per operation. This also bounds total request time.
            response = await asyncio.wait_for(
                self.http.post(ENDPOINT, json=payload), timeout=self.timeout,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise JevError("request_timeout") from None
        except httpx.HTTPError:
            raise JevError("transport_error") from None
        if response.status_code != 200:
            try:
                delay = float(response.headers.get("retry-after", "0"))
                if not math.isfinite(delay):
                    delay = 0
            except ValueError:
                delay = 0
            raise JevError(f"http_{response.status_code}", response.status_code, max(0, delay))
        try:
            data = response.json()
            answer = data["answers"][QUESTION]
            criteria = payload["questions"][QUESTION]["criteria"]
            if answer["type"] != "choice" or answer["choice"] not in criteria:
                raise ValueError("choice")
            confidence = answer["confidence"]
            probabilities = answer["probabilities"]
            if set(probabilities) != set(criteria):
                raise ValueError("probabilities")
            values = [confidence, *probabilities.values()]
            if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                raise ValueError("range")
            if not math.isclose(sum(probabilities.values()), 1, abs_tol=0.02):
                raise ValueError("distribution")
            usage = data.get("usage", {})
            if any(type(v) is not int or v < 0 for v in usage.values()):
                raise ValueError("usage")
            # Keep only documented, non-sensitive response fields.
            return {"model": data.get("model"), "answer": answer, "usage": usage}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise JevError("invalid_response") from None

    async def close(self):
        await self.http.aclose()


@dataclass
class Decision:
    request_id: int
    game_loop: int
    started_at: float
    action_id: int
    response: dict
    latency_ms: float
    plan_id: Optional[int] = None


class DecisionScheduler:
    """Game callbacks poll finished work; they never await an in-flight model call."""

    def __init__(self, client, emit: Callable, interval=1.0, max_age=4.0,
                 max_requests=2000, clock=time.monotonic, cadence_clock=None):
        if interval <= 0 or max_age <= 0 or max_requests < 1:
            raise ValueError("Decision interval, max age and request budget must be positive.")
        self.client, self.emit, self.clock = client, emit, clock
        self.cadence_clock = clock if cadence_clock is None else cadence_clock
        self.interval, self.max_age, self.max_requests = interval, max_age, max_requests
        self.task = None
        self.request_id = 0
        self.next_request_at = 0.0
        self.disabled = False
        self.disabled_status = None
        self.closed = False
        self.stats = Counter()
        self.latencies = []
        self.consecutive_errors = 0
        self.last_game_loop = 0
        self.pending_plan_id = None

    @property
    def ready(self):
        return (not self.closed and not self.disabled and self.task is None
                and self.request_id < self.max_requests and self.cadence_clock() >= self.next_request_at)

    def submit(self, game_loop: int, state: dict, choices: dict) -> bool:
        if not self.ready:
            return False
        # The live Bot state can change while the HTTP task is suspended.
        payload = copy.deepcopy(self.client.payload(state, choices))
        plan_id = (state.get("strategic_plan") or {}).get("plan_id")
        self.pending_plan_id = plan_id
        self.last_game_loop = game_loop
        self.request_id += 1
        request_id, started_at = self.request_id, self.clock()
        self.next_request_at = self.cadence_clock() + self.interval
        self.stats["requests"] += 1
        self.emit("request", request_id=request_id, game_loop=game_loop, plan_id=plan_id, payload=payload)

        async def request():
            response = await self.client.choose(payload)
            return Decision(request_id, game_loop, started_at,
                            int(response["answer"]["choice"]), response,
                            (self.clock() - started_at) * 1000, plan_id)

        self.task = asyncio.create_task(request())
        return True

    def poll(self, game_loop: int) -> Optional[Decision]:
        self.last_game_loop = game_loop
        if self.task is None or not self.task.done():
            return None
        task, self.task = self.task, None  # Consume each completed request exactly once.
        try:
            decision = task.result()
        except JevError as exc:
            self.stats["api_errors"] += 1
            self.consecutive_errors += 1
            # Never retry an old state. Back off and later submit a fresh observation.
            self.next_request_at = self.cadence_clock() + max(
                exc.retry_after, min(30, 2 ** min(self.consecutive_errors, 5)))
            self.disabled = exc.status in (400, 401, 402, 403, 404, 422)
            self.disabled_status = exc.status if self.disabled else None
            self.emit("api_error", request_id=self.request_id, error=str(exc),
                      game_loop=game_loop, plan_id=self.pending_plan_id,
                      status=exc.status, disabled=self.disabled,
                      error_category="billing" if exc.status == 402 else "configuration" if self.disabled else "transient",
                      retry_in_seconds=0 if self.disabled else max(0, self.next_request_at - self.cadence_clock()))
            return None
        self.consecutive_errors = 0
        self.stats["responses"] += 1
        self.stats["input_tokens"] += decision.response["usage"].get("input_tokens", 0)
        self.stats["output_tokens"] += decision.response["usage"].get("output_tokens", 0)
        self.latencies.append(decision.latency_ms)
        wall_age = self.clock() - decision.started_at
        game_age = (game_loop - decision.game_loop) / 22.4
        stale = wall_age > self.max_age or game_age > self.max_age or game_age < 0
        self.emit("response", request_id=decision.request_id, game_loop=game_loop,
                  plan_id=decision.plan_id,
                  observation_game_loop=decision.game_loop, latency_ms=decision.latency_ms,
                  wall_age_s=wall_age, game_age_s=game_age, stale=stale, **decision.response)
        if stale:
            self.stats["stale_results"] += 1
            return None
        return decision

    def require_service(self):
        """End an invalid trial on permanent API failure; never label it a loss."""
        if self.disabled:
            raise JevError(f"decision_service_unavailable:http_{self.disabled_status}", self.disabled_status)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.task is not None and self.task.done():
            decision = self.poll(self.last_game_loop)
            if decision is not None:
                self.stats["unconsumed_at_shutdown"] += 1
                self.emit("decision_discarded", request_id=decision.request_id, plan_id=decision.plan_id,
                          game_loop=self.last_game_loop, reason="shutdown_after_response")
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, JevError):
                pass
            self.stats["unconsumed_at_shutdown"] += 1
            self.emit("request_cancelled", request_id=self.request_id, plan_id=self.pending_plan_id,
                      game_loop=self.last_game_loop, reason="shutdown_inflight", usage_known=False)
        await self.client.close()
