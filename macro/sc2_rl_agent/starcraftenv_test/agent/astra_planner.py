"""Codex-authenticated strategic planning, independent of the SC2 callback loop."""

import asyncio
import copy
import json
import math
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from .macro_contract import ACTION_LIMITS, ARMY_ACTIONS


PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "objective": {"type": "string"},
        "goals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"action_id": {"type": "integer", "enum": list(range(60))},
                           "target": {"type": "integer"}},
            "required": ["action_id", "target"]}},
        "worker_target": {"type": "integer"},
        "base_target": {"type": "integer"},
        "allowed_spending_actions": {"type": "array", "items": {"type": "integer", "enum": list(range(60))}},
        "reserve_for_action": {"type": ["integer", "null"], "enum": [None, *range(60)]},
        "reserve_after_workers": {"type": "integer"},
        "army_posture": {"type": "string", "enum": ["defend", "attack", "retreat"]},
        "attack_min_army": {"type": "integer"},
        "retreat_below_army": {"type": "integer"},
        "min_posture_seconds": {"type": "integer"},
        "guidance": {"type": "string"},
        "priority_action": {"type": ["integer", "null"], "enum": [None, *range(71), 72]},
        "production_priority": {"type": "array", "items": {"type": "integer", "enum": list(range(60))}},
        "army_target_id": {"type": ["string", "null"]},
    },
}
PLAN_SCHEMA["required"] = list(PLAN_SCHEMA["properties"])

PLANNER_INSTRUCTIONS = """You are the strategic brain for a live StarCraft II Protoss bot.
Return only the requested JSON plan. All relevant facts are in the supplied JSON.
Do not use tools, inspect files, browse, write code, or ask questions.
Jev makes individual macro choices about once per second; you plan the next 120-180
game seconds. This is a REALTIME game: it continues while you think. Be concise.
Text limits count characters, including spaces and punctuation, NOT tokens.
Both objective and guidance must be nonempty and each MUST be at most 1200
characters; exceeding either limit rejects the entire plan. Aim for objective
at most 200 characters and guidance at most 900 characters to leave a margin.
Normal refreshes are 60 game seconds apart, with a 30-second execution window
after acceptance before ordinary events can refresh. Urgent threats bypass it.
Set production headroom for this horizon: tiny worker/army caps will stall Jev.
Use only action IDs in action_catalog (original 0-71 plus 72 MULTI-DEFEND).
goals are desired TOTAL ready plus
pending unit/building counts, or target=1 for starting an upgrade. Gateway counts
include Warpgates. Goals already satisfied stop consuming reserved resources.
Choose attainable goals with prerequisites, rather than an entire late-game build.
worker_target and base_target are HARD caps including pending production. Account
for actual mineral/gas saturation, remaining minerals and bases under construction.
Do not keep making workers on one saturated or mined-out base. Plan expansions
before minerals run out. Diversion into many unrelated tech buildings is harmful.
allowed_spending_actions lists production/building/research IDs 0-59 Jev MAY buy;
include necessary workers, supply, technology prerequisites and army production.
Do NOT put IDs 60-72 in goals or allowed_spending_actions. Scouting, Chronoboost,
army commands and waiting are available separately when legal. A goal may be
unaffordable now; Jev checks actual affordability. Each goal action MUST also be
in allowed_spending_actions, even if already completed. No duplicate action IDs.
Use 1-12 goals to cap counts. Research IDs 34-59 must have target=1.
Research with nonzero progress is started. worker_target is 0-76, base_target 1-8;
worker/base goals cannot exceed those caps. attack_min_army is 10-200,
retreat_below_army is 0-199 and strictly less than attack_min_army;
min_posture_seconds is 5-60. If last_plan_rejection is present, correct that error.
reserve_for_action is null or ONE production/building/research goal whose actual cost must be
saved. It activates when worker count reaches reserve_after_workers and the goal
is unmet. Other purchases cannot spend this reserve. It does NOT execute the goal
automatically. Include the reserved action in goals and allowed_spending_actions;
reserve only when its prerequisites are ready, so saving does not deadlock tech.
Urgent supply Pylons and combat production for an attacked base may override the
spending budget; everything must still be legal and affordable in the game.
army_posture guides Jev's attack/retreat/defend decisions. attack_min_army is the minimum
READY combat supply to allow an attack. retreat_below_army enables emergency retreat.
min_posture_seconds holds an army intent to avoid changing it every second, with
emergency retreat allowed. A defend plan permits withdrawal and local defense.
There is NO automatic production/build order. Local code distributes workers,
morphs Warpgates, carries out army intent, continues assigned expansion scouting,
and escorts the army with one Observer. Jev chooses all new
production, construction and research. Enemy knowledge is partial; last-seen
observations are historical and missing enemies are unknown. Supply army may
include pending units; resource.ready_army_supply excludes them and noncombat units.
Use outcomes/failures and your previous plan to adapt.
Every catalog item declares target_limit and reservation_blocked. Never exceed
target_limit. Temporary lack of money is not a technology failure. Impossible
reservations are suspended, and no build action requires an existing Nexus to rebuild one.
priority_action is one immediate next action or null. production_priority is an
ordered subset of goal action IDs, with the most urgent attainable goal first.
Use reserves when a crucial unit must precede optional purchases, e.g. first Immortal
before an extra Observer. Maintain mineral income; avoid excess early gas infrastructure.
army_target_id is null (automatic target selection) or an ID from navigation.targets.
Use 72 to restore defense after retreat. An executable change of army posture takes
priority over unfinished production goals: attack now once ready, then replace losses.
Production totals are replenishment ceilings, not a requirement to finish before attacking.
Do not demand all ceilings simultaneously at 200 supply. Do not delay an attack for
upgrades or optional static defenses. Supply forecasting permits early Pylons and power repair.
Capabilities: army attack-move to a remembered building, expansion search when cleared,
base defense/rally, retreat, scouting, and one Observer escort. Formation control,
spellcasting and specific-unit focus fire are NOT implemented; do not rely on prose
to make them happen. Oracle/Disruptor spell attacks and Warp Prism transport are not
implemented: do not spend on these as a fighting force. High/Dark Templar pairs can
merge into Archons using action 18; Psi Storm research does not make the bot cast it.
Carriers, Sentries and Motherships can follow ordinary army attack orders.
Enemy memory respects fog; unexplored expansion sites are unknown.
"""


class PlannerError(Exception):
    """A bounded, credential-free planner failure."""


ADVISORY_PLANNER_INSTRUCTIONS = """You provide strategic advice for a live StarCraft II Protoss bot.
Return exactly the JSON schema, with no tools or extra text. JEV independently
selects a macro action about once per second from the base executor's currently
available actions. This is a REALTIME game that continues while you think.
Plan the next 120-180 game seconds concisely. Normal refreshes are 60 game seconds
apart, with a 30-second execution window; urgent threats may refresh sooner.
Text limits count characters, including spaces and punctuation, NOT tokens.
Both objective and guidance must be nonempty and each MUST be at most 1200
characters; exceeding either limit rejects the entire plan. Aim for objective
at most 200 characters and guidance at most 900 characters to leave a margin.

Your plan is advice, not an action mask. Worker/base targets and goals are desired
totals, not hard ceilings. The legacy field allowed_spending_actions is a list of
suggested production/building/research actions, not an exclusive whitelist.
reserve_for_action and its budget are suggested savings, not locked resources.
army_posture, attack_min_army, retreat_below_army, min_posture_seconds,
priority_action and production_priority are recommendations that JEV may override
from current observations. No main order is enforced or automatically executed.
army_target_id is a suggested location for context; when JEV chooses attack, the
ordinary executor selects a destination using observed enemies and expansion search.

Use action IDs from action_catalog (0-71 plus 72 MULTI-DEFEND). Keep the existing
schema consistent: 1-12 distinct goals with TOTAL ready-plus-pending unit/building
counts; research goals have target=1. Gateway counts include Warpgates. Each goal
action must appear in allowed_spending_actions; both lists use IDs 0-59 only.
worker_target is 0-76 and base_target 1-8; worker/base goals must fit those suggested
targets. Do not exceed catalog target_limit. attack_min_army is 10-200;
retreat_below_army is 0-199 and smaller than attack_min_army; min_posture_seconds
is 5-60. reserve_for_action is null or a goal present in the spending list;
reserve_after_workers controls when saving is suggested. priority_action is null
or an available catalog action other than WAIT, and any production priority must
appear in the spending list. production_priority is an ordered subset of goals.
An army priority must agree with army_posture. army_target_id is null or a target
in navigation.targets; a defend plan uses home. Correct last_plan_rejection.

Explain your strategic objective, production direction, economy and reasons to
advance, defend or withdraw. Account for actual mineral/gas saturation, depleted
mineral patches, bases under construction, available technology and ready combat
supply. Avoid many unrelated technologies and excessive early gas. An attainable
attack need not wait for optional production goals or upgrades. A catalog item's
reservation_blocked describes a prerequisite problem, not temporary affordability.
Adapt to observed execution outcomes and changing enemy composition.

There is no automatic build order or production. Local code distributes workers,
morphs Warpgates, executes persistent army intent, continues assigned scouting,
and escorts the army with one Observer. JEV chooses all new production, buildings,
research and army intent. Enemy knowledge respects fog; historical sightings may
include dead units. resource.ready_army_supply excludes pending/noncombat units.
Capabilities are attack-move, observed-enemy target selection, expansion search,
base defense, retreat and scouting. Formation control, spellcasting, specific-unit
focus fire and Warp Prism transport are not implemented. Do not rely on Oracle or
Disruptor spell attacks, Psi Storm casting, or transport. High/Dark Templar pairs
can merge into Archons with action 18. Ordinary attack orders work for Carriers,
Sentries and Motherships. Supply forecasting supports early Pylons and power repair.
"""


def validate_plan(value, catalog=None, target_ids=None):
    if not isinstance(value, dict) or set(value) != set(PLAN_SCHEMA["required"]):
        raise PlannerError("invalid_plan_fields")
    for key in ("objective", "guidance"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 1200:
            raise PlannerError("invalid_plan_text")
    ranges = {"worker_target": (0, 76), "base_target": (1, 8),
              "reserve_after_workers": (0, 76), "attack_min_army": (10, 200),
              "retreat_below_army": (0, 200), "min_posture_seconds": (5, 60)}
    for key, (low, high) in ranges.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise PlannerError("invalid_plan_" + key)
    if value["retreat_below_army"] >= value["attack_min_army"]:
        raise PlannerError("retreat_threshold_must_be_below_attack_threshold")
    if value["army_posture"] not in ("defend", "attack", "retreat"):
        raise PlannerError("invalid_army_posture")
    allowed = value["allowed_spending_actions"]
    if not isinstance(allowed, list) or not allowed or len(allowed) > 60:
        raise PlannerError("invalid_spending_actions")
    if any(type(a) is not int or not 0 <= a <= 59 for a in allowed) or len(set(allowed)) != len(allowed):
        raise PlannerError("invalid_spending_action_id")
    goals = value["goals"]
    if not isinstance(goals, list) or not 1 <= len(goals) <= 12:
        raise PlannerError("invalid_goals")
    seen = set()
    for goal in goals:
        if not isinstance(goal, dict) or set(goal) != {"action_id", "target"}:
            raise PlannerError("invalid_goal_fields")
        action, target = goal["action_id"], goal["target"]
        if type(action) is not int or action not in allowed or action in seen:
            raise PlannerError("invalid_goal_action")
        if type(target) is not int or not 1 <= target <= (1 if action >= 34 else 200):
            raise PlannerError("invalid_goal_target")
        if target > ACTION_LIMITS[action]:
            raise PlannerError(f"goal_exceeds_executor_limit_action_{action}_max_{ACTION_LIMITS[action]}")
        if catalog and catalog.get(str(action), {}).get("exists_in_game_version") is False:
            raise PlannerError(f"goal_unavailable_in_game_version_action_{action}")
        if action == 0 and target > value["worker_target"]:
            raise PlannerError("worker_goal_exceeds_cap")
        if action == 21 and target > value["base_target"]:
            raise PlannerError("base_goal_exceeds_cap")
        seen.add(action)
    reserve = value["reserve_for_action"]
    if reserve is not None and (type(reserve) is not int or not 0 <= reserve <= 59 or reserve not in seen):
        raise PlannerError("invalid_reservation")
    if reserve is not None and value["reserve_after_workers"] > value["worker_target"]:
        raise PlannerError("reservation_cannot_activate")
    priority = value["priority_action"]
    if priority is not None:
        if type(priority) is not int or priority not in {*range(71), 72}:
            raise PlannerError("invalid_priority_action")
        if priority < 60 and priority not in allowed:
            raise PlannerError("priority_action_not_allowed")
        if priority in ARMY_ACTIONS and ARMY_ACTIONS[priority] != value["army_posture"]:
            raise PlannerError("priority_action_conflicts_with_posture")
    production = value["production_priority"]
    if (not isinstance(production, list) or any(type(a) is not int or a not in seen for a in production)
            or len(set(production)) != len(production)):
        raise PlannerError("invalid_production_priority")
    target = value["army_target_id"]
    if target is not None and (not isinstance(target, str) or not 1 <= len(target) <= 80
                               or (target_ids is not None and target not in target_ids)):
        raise PlannerError("invalid_army_target_id")
    if value["army_posture"] != "attack" and target not in (None, "home"):
        raise PlannerError("defense_target_must_be_home")
    return copy.deepcopy(value)


def codex_failure_category(stdout, stderr):
    """Return only an allowlisted category; never forward arbitrary account output."""
    text = (stdout[-16000:] + stderr[-16000:]).lower()
    categories = {
        "authentication": ("unauthorized", "authentication", "token expired", "not logged in"),
        "rate_limit": ("rate limit", "rate_limit", "quota exceeded", "usage limit"),
        "capacity": ("overloaded", "capacity", "temporarily unavailable"),
        "network": ("connection reset", "connection refused", "network error", "tls error"),
        "context_limit": ("context length", "context window"),
        "invalid_schema": ("invalid schema", "invalid_json_schema"),
    }
    return next((name for name, patterns in categories.items() if any(p in text for p in patterns)), "unknown")


def find_codex(path=None):
    candidate = Path(path) if path else None
    if candidate is None:
        found = shutil.which("codex")
        candidate = Path(found) if found else None
    if os.name == "nt" and (candidate is None or candidate.suffix.lower() in (".cmd", ".ps1", ".bat")):
        npm = candidate.parent if candidate else Path(os.environ.get("APPDATA", "")) / "npm"
        matches = list((npm / "node_modules" / "@openai" / "codex").glob("**/codex.exe"))
        if matches:
            candidate = matches[0]
    if candidate is None or not candidate.is_file() or candidate.suffix.lower() in (".cmd", ".bat", ".ps1"):
        raise ValueError("Codex executable not found; pass --codex-path pointing to the native codex executable.")
    return candidate.resolve()


class CodexPlannerClient:
    """Use saved Codex login; never read/copy auth.json or pass credentials to SC2."""

    def __init__(self, output_dir, executable=None, model="gpt-6-astra", timeout=60, effort="medium",
                 plan_mode="constrained"):
        if plan_mode not in {"constrained", "advisory"}:
            raise ValueError("plan_mode must be constrained or advisory")
        self.plan_mode = plan_mode
        self.executable = find_codex(executable)
        self.model, self.timeout, self.effort = model, timeout, effort
        self.directory = Path(output_dir) / "planner"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.schema_path = self.directory / "plan.schema.json"
        self.schema_path.write_text(json.dumps(PLAN_SCHEMA), encoding="utf-8")
        self.workdir = self.directory / "workspace"
        self.workdir.mkdir(exist_ok=True)
        self._process = None
        self._lock = threading.Lock()
        self._closed = False

    def _command(self, result_path):
        return [str(self.executable), "exec", "--ignore-user-config", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--model", self.model,
                "--disable", "apps", "--disable", "shell_tool", "--disable", "hooks",
                "--disable", "browser_use", "--disable", "computer_use",
                "--enable", "respect_system_proxy", "--disable", "unbounded_connection_retries",
                "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                "-c", f'model_reasoning_effort="{self.effort}"',
                "--output-schema", str(self.schema_path), "--output-last-message", str(result_path),
                "--json", "--color", "never", "-"]

    def _run(self, request_id, payload):
        result_path = self.directory / f"plan-{request_id:04d}.json"
        if result_path.exists():
            raise PlannerError("planner_output_already_exists")
        instructions = ADVISORY_PLANNER_INSTRUCTIONS if self.plan_mode == "advisory" else PLANNER_INSTRUCTIONS
        prompt = (instructions + "\nREQUIRED JSON SCHEMA (same schema supplied via --output-schema):\n"
                  + json.dumps(PLAN_SCHEMA) + "\nINPUT JSON:\n" + json.dumps(payload, ensure_ascii=False))
        # Store exactly the prompt sent to Codex next to its original JSON reply.
        (self.directory / f"request-{request_id:04d}.txt").write_text(prompt, encoding="utf-8")
        try:
            with self._lock:
                if self._closed:
                    raise PlannerError("planner_closed")
                process = subprocess.Popen(self._command(result_path), cwd=self.workdir,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding="utf-8",
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self._process = process
            try:
                stdout, stderr = process.communicate(prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise PlannerError("codex_timeout") from None
            if process.returncode != 0:
                # Do not propagate CLI stderr, which may include account information.
                raise PlannerError(f"codex_exit_{process.returncode}:{codex_failure_category(stdout, stderr)}")
            usage, completed = {}, False
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed":
                    completed, usage = True, event.get("usage", {})
                if event.get("type") in ("error", "turn.failed"):
                    raise PlannerError("codex_turn_failed")
                item_type = event.get("item", {}).get("type")
                if item_type in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                    raise PlannerError("unexpected_planner_tool_use")
            if not completed or not result_path.is_file():
                raise PlannerError("missing_codex_result")
            usage = {k: v for k, v in usage.items() if type(v) is int and v >= 0}
            # A rejected plan still consumed tokens. Return its usage to the scheduler
            # before reporting validation failure; never activate invalid output.
            try:
                raw_plan = json.loads(result_path.read_text(encoding="utf-8"))
            except ValueError:
                return {"plan": {}, "usage": usage, "model": self.model,
                        "validation_error": "invalid_plan_json"}
            try:
                targets = payload.get("state", {}).get("navigation", {}).get("targets")
                plan = validate_plan(raw_plan, payload.get("action_catalog"),
                                     {t["id"] for t in targets} if targets is not None else None)
            except PlannerError as exc:
                return {"plan": raw_plan if isinstance(raw_plan, dict) else {},
                        "usage": usage, "model": self.model, "validation_error": str(exc)}
            return {"plan": plan, "usage": usage, "model": self.model}
        except (OSError, ValueError) as exc:
            raise PlannerError("codex_io_or_json_error") from None
        finally:
            with self._lock:
                self._process = None

    async def plan(self, request_id, payload):
        # BurnySC2/nest_asyncio uses a Windows loop without async subprocess support.
        return await asyncio.to_thread(self._run, request_id, copy.deepcopy(payload))

    async def close(self):
        with self._lock:
            self._closed = True
            if self._process is not None and self._process.poll() is None:
                self._process.kill()


class StrategicPlanner:
    URGENT_EVENTS = {"base_attacked", "base_lost", "army_losses", "new_enemy_threat"}
    RECOVERY_EVENTS = {"invalid_plan", "plan_expired", "opening"}

    def __init__(self, client, emit, interval=60, ttl=180, max_age=60, max_requests=80, clock=time.monotonic,
                 min_interval=10, event_cooldown=30, execution_window=30):
        if any(not math.isfinite(v) or v <= 0 for v in (interval, ttl, max_age, min_interval, event_cooldown, execution_window)) or max_requests < 1:
            raise ValueError("Planner intervals, lifetime and request budget must be positive.")
        self.client, self.emit, self.clock = client, emit, clock
        self.interval, self.ttl, self.max_age, self.max_requests = interval, ttl, max_age, max_requests
        self.min_interval, self.event_cooldown = min_interval, event_cooldown
        self.execution_window = execution_window
        self.ordinary_events_after = 0
        self._request_counts = {}
        self.task = None
        self.active = None
        self.request_id = 0
        self.next_game_time = 0
        self.next_wall_time = 0
        self.dirty = set()
        self.trigger_details = {}
        self._last_trigger = {}
        self.urgent_revision = 0
        self._budget_reported = False
        self.stats = Counter()
        self.latencies = []
        self.closed = False
        self.last_game_loop = 0
        self.last_rejection = None

    def trigger(self, reason, **details):
        now = self.clock()
        if reason in self.dirty:
            self.trigger_details[reason] = details
            return False
        if now - self._last_trigger.get(reason, -math.inf) < self.event_cooldown:
            self.stats["triggers_suppressed"] += 1
            return False
        self._last_trigger[reason] = now
        self.dirty.add(reason)
        self.trigger_details[reason] = details
        if reason in self.URGENT_EVENTS:
            self.urgent_revision += 1
        self.stats["triggers"] += 1
        self.emit("planner_trigger", game_loop=self.last_game_loop, reason=reason, details=details,
                  while_inflight=self.task is not None)
        return True

    def poll(self, game_loop):
        """Consume a completed plan every game callback, without awaiting inference."""
        self.last_game_loop = game_loop
        game_time = game_loop / 22.4
        if self.active and game_time >= self.active["expires_game_seconds"]:
            self.emit("plan_expired", plan_id=self.active["plan_id"], game_loop=game_loop)
            self.stats["expired"] += 1
            self.active = None
            self.trigger("plan_expired")
        self._consume(game_loop)

    def clear_trigger(self, reason, why):
        if reason in self.dirty:
            self.dirty.remove(reason)
            details = self.trigger_details.pop(reason, {})
            self.stats["triggers_rebased"] += 1
            self.emit("planner_trigger_cleared", game_loop=self.last_game_loop,
                      reason=reason, why=why, details=details)

    def tick(self, game_loop, snapshot, catalog):
        self.poll(game_loop)
        now, game_time = self.clock(), game_loop / 22.4
        if self.request_id >= self.max_requests and not self._budget_reported:
            self._budget_reported = True
            self.emit("planner_budget_exhausted", game_loop=game_loop, max_requests=self.max_requests)
        bypass = bool(self.dirty & (self.URGENT_EVENTS | self.RECOVERY_EVENTS))
        window_open = self.active is None or game_time >= self.ordinary_events_after
        due = bypass or (window_open and (game_time >= self.next_game_time or self.dirty))
        if (not self.closed and self.task is None and self.request_id < self.max_requests
                and now >= self.next_wall_time and due):
            self.request_id += 1
            request_id, urgent_revision = self.request_id, self.urgent_revision
            triggers = sorted(self.dirty) or (["opening"] if request_id == 1 else ["periodic"])
            payload = copy.deepcopy({"state": snapshot, "action_catalog": catalog,
                                     "previous_plan": self.active, "triggers": triggers,
                                     "trigger_details": self.trigger_details,
                                     "last_plan_rejection": self.last_rejection,
                                     "request_context": {"game_seconds": game_time,
                                                         "periodic_interval_game_seconds": self.interval,
                                                         "execution_window_game_seconds": self.execution_window,
                                                         "plan_ttl_game_seconds": self.ttl}})
            self._request_counts = {str(a): item.get("count_with_pending", 0) for a, item in catalog.items()}
            self.dirty.clear()
            self.trigger_details = {}
            self.next_game_time, self.next_wall_time = game_time + self.interval, now + self.min_interval
            self.stats["requests"] += 1
            self.emit("planner_request", request_id=request_id, game_loop=game_loop,
                      model=self.client.model, payload=payload)

            async def request():
                result = await self.client.plan(request_id, payload)
                return result, game_loop, now, (self.clock() - now) * 1000, urgent_revision

            self.task = asyncio.create_task(request())

    def _consume(self, game_loop, apply_plan=True):
        if self.task is None or not self.task.done():
            return
        task, self.task = self.task, None
        now, game_time = self.clock(), game_loop / 22.4
        try:
            result, observation_loop, started, latency_ms, urgent_revision = task.result()
            wall_age, game_age = now - started, (game_loop - observation_loop) / 22.4
            stale = max(wall_age, game_age) > self.max_age or game_age < 0
            superseded = urgent_revision < self.urgent_revision
            self.stats["responses"] += 1
            self.latencies.append(latency_ms)
            for key, value in result["usage"].items():
                self.stats[key] += value
            self.emit("planner_response", request_id=self.request_id, game_loop=game_loop,
                      observation_game_loop=observation_loop, latency_ms=latency_ms,
                      wall_age_s=wall_age, game_age_s=game_age, stale=stale,
                      context_superseded=superseded, **result)
            if result.get("validation_error"):
                self.stats["invalid_plans"] += 1
                self.last_rejection = result["validation_error"]
                if apply_plan:
                    self.trigger("invalid_plan")
                raise PlannerError(result["validation_error"])
            if not apply_plan:
                self.stats["unconsumed_at_shutdown"] += 1
                self.emit("planner_discarded", request_id=self.request_id, game_loop=game_loop,
                          reason="shutdown_after_response")
            elif superseded:
                self.stats["superseded"] += 1
                self.emit("planner_discarded", request_id=self.request_id, game_loop=game_loop,
                          reason="battlefield_changed_during_planning")
            elif stale:
                self.stats["stale"] += 1
                self.emit("planner_stale", request_id=self.request_id, game_loop=game_loop,
                          wall_age_s=wall_age, game_age_s=game_age)
            else:
                self.last_rejection = None
                # Progress of the replaced plan must not immediately refresh its successor.
                self.clear_trigger("goal_completed", "new_plan_replaces_goal_context")
                self.ordinary_events_after = game_time + self.execution_window
                self.active = {"plan_id": self.request_id, "accepted_game_seconds": game_time,
                               "observation_game_seconds": observation_loop / 22.4,
                               "expires_game_seconds": game_time + self.ttl,
                               "goal_counts_at_observation": {
                                   str(g["action_id"]): self._request_counts.get(str(g["action_id"]), 0)
                                   for g in result["plan"]["goals"]}, **result["plan"]}
                self.stats["accepted"] += 1
                self.emit("plan_accepted", request_id=self.request_id, game_loop=game_loop, latency_ms=latency_ms,
                          observation_game_loop=observation_loop, usage=result["usage"], plan=self.active)
        except PlannerError as exc:
            self.stats["errors"] += 1
            self.next_wall_time = now + self.min_interval
            self.emit("planner_error", request_id=self.request_id, game_loop=game_loop, error=str(exc))

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self._consume(self.last_game_loop, apply_plan=False)
        await self.client.close()
        self._consume(self.last_game_loop, apply_plan=False)
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, PlannerError):
                pass
            self.stats["unconsumed_at_shutdown"] += 1
            self.emit("planner_cancelled", request_id=self.request_id, game_loop=self.last_game_loop,
                      reason="shutdown_inflight", usage_known=False)
