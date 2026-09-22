"""One Codex-authenticated Astra plan per map, reused by fixed-step JEV."""

import asyncio
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from .catalog import map_param_registry
from .policy import TEAM_CORE_FIELDS, ORDER_FIELDS, ENEMY_FIELDS, BATCH_RELATION_FIELDS


HIERARCHY_SCHEMA_VERSION = 7
TEXT_FIELDS = ("objective", "opening", "target_selection", "movement_and_cooldowns")
LIST_FIELDS = ("priorities", "contingencies", "assumptions")
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "map": {"type": "string"},
        **{key: {"type": "string"} for key in TEXT_FIELDS},
        **{key: {"type": "array", "items": {"type": "string"}} for key in LIST_FIELDS},
        "unit_roles": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"unit_type": {"type": "string"}, "guidance": {"type": "string"}},
            "required": ["unit_type", "guidance"]}},
    },
}
PLAN_SCHEMA["required"] = list(PLAN_SCHEMA["properties"])

INSTRUCTIONS = """You are GPT-6 Astra, the tactical planner for SMAC-Hard StarCraft II micro.
Return only the requested JSON plan. Do not use tools, inspect files, browse,
write code, or ask questions. All supplied facts are in INPUT JSON.
Create ONE concise map-level plan for JEV to execute across three independent
episodes on this map. You will NOT be called again during combat. Write in English,
about 250-450 words, at most 6000 JSON UTF-8 bytes. Each text field <=1000 characters;
each list <=6 entries, each entry <=500 characters; unit_roles <=12 entries.
Describe conditional tactics and unit-type roles, not a sequence of per-frame
actions or instructions tied to ephemeral unit tags, game_loop values, or one
episode's positions. JEV sees the full plan alongside its current observation on
EVERY decision. The initial observation is a snapshot, not enduring ground truth.
SC2 realtime=False: the whole game pauses while either model thinks; after JEV's
team decision, both teams advance exactly step_mul game loops. JEV chooses one
legal action per living ally; small teams share one HTTP request, larger teams
share a snapshot but are batched. Units cannot see other units' pending choices.
Give deterministic coordination rules using the supplied features where helpful.
Actions are fixed: 1 Stop cancels the current order; 2 north/+y, 3 south/-y,
4 east/+x, 5 west/-x move by move_amount; 6+j attacks visible enemy e{j}.
Medivacs instead use 6+j to heal allied u{j}, only if a legal heal candidate exists.
0 is dead-unit no-op, not a choice for living units. There is no hold-previous-order
choice. An attack order approaches if needed and fires when ready. Available attack
does NOT mean already in weapon range. Movement can interrupt attacks. Abilities,
spells, transformations, blink, stim, burrow, siege mode and arbitrary coordinates
are unavailable. No production, economy, reinforcements, or macro bot is involved.
Use current health/shields, weapon cooldown, ranges, target compatibility, distances,
energy and candidate endpoints. Do not invent measurements absent from the input.
Only shared allied vision is provided; unseen enemies are unknown, not dead.
scenario_brief supplies public fixed map rosters, spawn anchors, verified opponent
configuration, exact static walkable spans, components and height spans. These are
map priors, never current hidden enemy positions. Unknown fields remain unknown.
Attack criteria retain the SMAC center-distance cap of 6 even when native range
is longer; range null means unknown. A ready weapon in range can still have no
legal attack choice. Large batches preserve team_core columns for ALL allies,
including cooldown, maximum health/shield, energy, recent damage and current orders,
plus per-enemy legal/ready coverage counts; local relations and candidate endpoints
exist only for batch actors. No same-frame reservations or shared pending choices.
last_action_result supplies previous SC2 acceptance/rejection and observation
errors; acceptance does not prove firing. Order records retain raw IDs/tags and
human-readable actions/targets. Do not rely on missing teammate local relations,
precise future motion, attack periods, upgrades/buff damage, or hidden state.
Recommend only tactics possible through this action
space. JEV selects all concrete actions; the plan is guidance, not a local script.
"""


class PlannerError(RuntimeError):
    pass


def validate_plan(value, map_name):
    if not isinstance(value, dict) or set(value) != set(PLAN_SCHEMA["required"]):
        raise PlannerError("planner_invalid_fields")
    if value["map"] != map_name:
        raise PlannerError("planner_wrong_map")
    def valid_text(text, limit):
        return isinstance(text, str) and 0 < len(text.strip()) <= limit
    if any(not valid_text(value[key], 1000) for key in TEXT_FIELDS):
        raise PlannerError("planner_invalid_text")
    for key in LIST_FIELDS:
        if (not isinstance(value[key], list) or len(value[key]) > 6
                or any(not valid_text(item, 500) for item in value[key])):
            raise PlannerError("planner_invalid_" + key)
    roles = value["unit_roles"]
    if not isinstance(roles, list) or not 1 <= len(roles) <= 12:
        raise PlannerError("planner_invalid_roles")
    for role in roles:
        if (not isinstance(role, dict) or set(role) != {"unit_type", "guidance"}
                or not valid_text(role["unit_type"], 80) or not valid_text(role["guidance"], 500)):
            raise PlannerError("planner_invalid_role")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 6000:
        raise PlannerError("planner_plan_too_long")
    return copy.deepcopy(value)


def planning_input(payload, env):
    params = map_param_registry[env.map_name]
    return {"scenario": {"map": env.map_name, **{key: params[key] for key in
            ("n_agents", "n_enemies", "a_race", "b_race", "map_type")}},
            "control": {"realtime": False, "step_mul": env._step_mul,
                        "episode_limit": env.episode_limit, "move_amount": env._move_amount,
                        "map_size": [env.map_x, env.map_y], "use_ability": False},
            "scenario_brief": copy.deepcopy(getattr(env, "scenario_brief", {"status": "unknown"})),
            "jev_batch_contract": {
                "allies": list(TEAM_CORE_FIELDS), "visible_enemies": list(ENEMY_FIELDS),
                "orders": list(ORDER_FIELDS), "relations": list(BATCH_RELATION_FIELDS),
                "encoding": "Large batches use team_core rows in the listed column order. alive/weapon_ready=0/1. "
                            "orders lists index team_core.order_table. unit_type_id resolves via unit_types/profiles. "
                            "Local relations and movements also use named columns; range status distinguishes in/out/unknown/incompatible. "
                            "All team feedback is retained; full previous commands and full own unit records are present for batch actors."},
            "initial_jev_request": copy.deepcopy(payload)}


def attach_plan(payload, active):
    """Preserve every legal candidate; JEV alone chooses the concrete actions."""
    if payload["state"]["map"] != active["plan"]["map"]:
        raise PlannerError("planner_wrong_map")
    result = copy.deepcopy(payload)
    result["state"].update(schema_version=HIERARCHY_SCHEMA_VERSION, realtime=False,
                           strategic_plan=copy.deepcopy(active))
    for question in result["questions"].values():
        question["instructions"] += (
            " Use state.strategic_plan.plan as the map-level tactical plan. "
            "Apply its conditional priorities to the CURRENT observation and this unit's role. "
            "Choose only from this question's legal criteria; current facts override obsolete assumptions.")
    return result


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
        raise PlannerError("planner_native_codex_not_found")
    return candidate.resolve()


class MapPlanner:
    def __init__(self, directory, emit, model="gpt-6-astra", effort="medium", timeout=120, executable=None):
        self.executable = find_codex(executable)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.workdir = self.directory / "workspace"
        self.workdir.mkdir()
        self.schema_path = self.directory / "plan.schema.json"
        self.schema_path.write_text(json.dumps(PLAN_SCHEMA), encoding="utf-8")
        self.emit, self.model, self.effort, self.timeout = emit, model, effort, timeout
        self.active = None
        self.stats = {"model": model, "effort": effort, "requests": 0, "successful_responses": 0,
                      "errors": 0, "usage": {}, "scope": "one plan per map, reused across episodes"}
        self._process, self._closed = None, False
        self._lock = threading.Lock()

    def command(self, output):
        return [str(self.executable), "exec", "--ignore-user-config", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--model", self.model,
                "--disable", "apps", "--disable", "shell_tool", "--disable", "hooks",
                "--disable", "browser_use", "--disable", "computer_use",
                "--enable", "respect_system_proxy", "--disable", "unbounded_connection_retries",
                "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                "-c", f'model_reasoning_effort="{self.effort}"',
                "--output-schema", str(self.schema_path), "--output-last-message", str(output),
                "--json", "--color", "never", "-"]

    def _request(self, prompt):
        result_path = self.directory / "plan.json"
        if result_path.exists():
            raise PlannerError("planner_output_already_exists")
        try:
            with self._lock:
                if self._closed:
                    raise PlannerError("planner_closed")
                process = subprocess.Popen(self.command(result_path), cwd=self.workdir,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self._process = process
            try:
                stdout, _stderr = process.communicate(prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, _stderr = process.communicate()
                return {"stdout": stdout, "plan_text": "", "error": "planner_timeout"}
            return {"stdout": stdout,
                    "plan_text": result_path.read_text(encoding="utf-8") if result_path.exists() else "",
                    "error": f"planner_codex_exit_{process.returncode}" if process.returncode else None}
        except OSError:
            raise PlannerError("planner_io_error") from None
        finally:
            with self._lock:
                self._process = None

    async def get_plan(self, payload, env):
        if self.active is not None:
            if self.active["plan"]["map"] != env.map_name:
                raise PlannerError("planner_wrong_map")
            return copy.deepcopy(self.active)
        data = planning_input(payload, env)
        prompt = INSTRUCTIONS + "\nINPUT JSON:\n" + json.dumps(data, ensure_ascii=False)
        self.stats["requests"] += 1
        self.emit("planner_request", request_id=self.stats["requests"], model=self.model,
                  effort=self.effort, prompt=prompt, output_schema=PLAN_SCHEMA)
        started = time.monotonic()
        try:
            raw = await asyncio.to_thread(self._request, prompt)
            latency = (time.monotonic() - started) * 1000
            self.emit("planner_response", request_id=self.stats["requests"], model=self.model,
                      latency_ms=latency, **raw)
            completed, usage, failed = False, {}, raw["error"]
            for line in raw["stdout"].splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed":
                    completed, usage = True, event.get("usage", {})
                if event.get("type") in ("error", "turn.failed"):
                    failed = failed or "planner_turn_failed"
                if event.get("item", {}).get("type") in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                    failed = "planner_unexpected_tool_use"
            self.stats.update(usage=usage, latency_ms=latency)
            if failed or not completed or not raw["plan_text"]:
                raise PlannerError(failed or "planner_missing_result")
            try:
                value = json.loads(raw["plan_text"])
            except ValueError:
                raise PlannerError("planner_invalid_json") from None
            plan = validate_plan(value, env.map_name)
            digest = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            self.active = {"model": self.model, "plan_sha256": digest, "plan": plan}
            self.stats.update(successful_responses=1, plan_sha256=digest,
                              plan_file=str(self.directory / "plan.json"))
            self.emit("plan_accepted", **copy.deepcopy(self.active))
            return copy.deepcopy(self.active)
        except PlannerError as exc:
            self.stats["errors"] += 1
            self.emit("planner_error", error=str(exc))
            raise

    async def close(self):
        with self._lock:
            self._closed = True
            if self._process is not None and self._process.poll() is None:
                self._process.kill()


def add_planner_arguments(parser):
    def timeout(value):
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise argparse.ArgumentTypeError("planner timeout must be finite and positive")
        return result
    parser.add_argument("--planner", choices=("none", "codex"), default="none")
    parser.add_argument("--planner-model", default="gpt-6-astra")
    parser.add_argument("--planner-effort", choices=("low", "medium", "high", "xhigh", "max"), default="medium")
    parser.add_argument("--planner-timeout", type=timeout, default=120)
    parser.add_argument("--codex-path", type=Path)
