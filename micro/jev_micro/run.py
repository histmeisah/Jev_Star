"""Run Jev on SMAC-Hard maps with fixed-step, team-visible observations."""

import argparse
import asyncio
import hashlib
import json
import os
import random
import shutil
import statistics
import time
from datetime import datetime
from pathlib import Path

from .client import JevClient, JevError, load_api_key
from .policy import SCHEMA_VERSION, INTERFACE_VERSION, SUPPORTED_MAPS, batch_requests, build_request, decode_actions, nearest_actions
from .catalog import map_source, DEVELOPMENT_MAPS
from .environment import JevMicroEnv, opponent_for
from .planner import MapPlanner, HIERARCHY_SCHEMA_VERSION, attach_plan, add_planner_arguments
from .feedback import action_feedback
from .scenario import BRIEF_VERSION, build_scenario_brief


REPO = Path(__file__).resolve().parents[1]
UPSTREAM_COMMIT = "48d77ec8ca2e2769fb81299f298ea438ce54fc9d"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def installation_info(sc2_path):
    """Record the installed region, not the game's unrelated language locale."""
    build_info = sc2_path / ".build.info"
    if not build_info.is_file():
        return {"client_branch": "unknown", "installed_version": "unknown"}
    lines = build_info.read_text(encoding="utf-8-sig").splitlines()
    keys = [column.split("!")[0] for column in lines[0].split("|")]
    for line in lines[1:]:
        row = dict(zip(keys, line.split("|")))
        if row.get("Active") == "1":
            return {"client_branch": row.get("Branch", "unknown"),
                    "installed_version": row.get("Version", "unknown")}
    return {"client_branch": "unknown", "installed_version": "unknown"}


def install_map(sc2_path, map_name="3m"):
    if map_name not in SUPPORTED_MAPS:
        raise ValueError("Unsupported map.")
    source = map_source(map_name)
    target = sc2_path / "Maps" / "new_maps" / f"{map_name}.SC2Map"
    content = source.read_bytes()
    if target.exists() and target.read_bytes() != content:
        raise ValueError(f"Existing map differs from SMAC-Hard: {target}")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return hashlib.sha256(content).hexdigest()


def battle_state(env):
    # Evaluation only: these global outcome fields never enter the Jev payload.
    return {
        "allies_alive": sum(u.health > 0 for u in env.agents.values()),
        "enemies_alive": sum(u.health > 0 for u in env.enemies.values()),
        "allied_health": round(sum(u.health for u in env.agents.values()), 2),
        "enemy_health": round(sum(u.health for u in env.enemies.values()), 2),
        "allied_shield": round(sum(u.shield for u in env.agents.values() if u.health > 0), 2),
        "enemy_shield": round(sum(u.shield for u in env.enemies.values() if u.health > 0), 2),
    }


async def run(args, api_key):
    import numpy as np
    import pysc2
    from absl import flags, logging

    if Path(pysc2.__file__).resolve().parent != REPO / "pysc2":
        raise RuntimeError("This runner must use SMAC-Hard's vendored pysc2.")
    if not flags.FLAGS.is_parsed():
        flags.FLAGS(["jev-micro", "--sc2_timeout=45"])
    logging.set_verbosity(logging.WARNING)
    random.seed(args.seed)
    np.random.seed(args.seed)
    os.environ["SC2PATH"] = str(args.sc2_path)
    map_sha = install_map(args.sc2_path, args.map)
    output = (args.output_dir or REPO / "jev_runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
                if k != "config_file"}
    settings.update(map=args.map, realtime=False, visibility="team_visible",
                    interface_version=INTERFACE_VERSION, scenario_brief_version=BRIEF_VERSION,
                    scenario_prior="same compact public prior for pure JEV and Astra+JEV; full terrain for one-time planner",
                    use_ability=False, upstream_commit=UPSTREAM_COMMIT,
                    schema_version=HIERARCHY_SCHEMA_VERSION if args.planner == "codex" else SCHEMA_VERSION,
                    requested_episodes=args.episodes,
                    pysc2_path=pysc2.__file__, map_sha256=map_sha,
                    map_source=str(map_source(args.map).relative_to(REPO)),
                    development_map=args.map in DEVELOPMENT_MAPS,
                    connection_mode="local_sc2_api", **installation_info(args.sc2_path))
    source_files = ("jev_micro/client.py", "jev_micro/policy.py", "jev_micro/run.py", "jev_micro/planner.py",
                    "jev_micro/catalog.py", "jev_micro/environment.py", "jev_micro/scenario.py", "jev_micro/feedback.py",
                    "smac_hard/env/starcraft2/starcraft2.py", "pysc2/lib/remote_controller.py")
    settings["source_sha256"] = {p: hashlib.sha256((REPO / p).read_bytes()).hexdigest() for p in source_files}
    write_json(output / "run.json", settings)
    print(f"SMAC-Hard {args.map} | policy={args.policy} | opponent={args.opponent} | fixed-step={args.step_mul}", flush=True)
    print(f"Output: {output}", flush=True)
    print(f"Client branch: {settings['client_branch']} | local SC2 API (no Battle.net matchmaking)", flush=True)

    log_file = (output / "events.jsonl").open("w", encoding="utf-8", buffering=1)
    def emit(event, **fields):
        log_file.write(json.dumps({"event": event, **fields}, ensure_ascii=False, allow_nan=False) + "\n")

    env = JevMicroEnv(map_name=args.map, step_mul=args.step_mul, seed=args.seed,
                      use_ability=False, continuing_episode=True)
    client = JevClient(api_key, args.request_timeout) if args.policy == "jev" else None
    episodes, latencies = [], []
    requests = input_tokens = output_tokens = api_errors = action_errors = 0
    fixed_step_checks = 0
    planner_fixed_step_checks = 0
    planner, active_plan = None, None
    started = time.monotonic()
    failure = None
    semaphore = asyncio.Semaphore(args.batch_concurrency)

    async def fetch_batch(payload, episode, step, batch_id, record):
        nonlocal requests, input_tokens, output_tokens, api_errors
        async with semaphore:
            for attempt in range(args.api_retries + 1):
                if requests >= args.max_requests:
                    raise JevError("request_budget_exhausted")
                requests += 1
                record["requests"] += 1
                request_id = requests
                emit("model_request", episode=episode, step=step, batch_id=batch_id,
                     attempt=attempt + 1, request_id=request_id, payload=payload)
                request_start = time.monotonic()
                try:
                    response = await client.choose(payload)
                except JevError as exc:
                    api_errors += 1
                    emit("api_error", episode=episode, step=step, batch_id=batch_id,
                         request_id=request_id, error=str(exc), attempt=attempt + 1)
                    retryable = str(exc) in {"request_timeout", "transport_error", "http_429",
                                            "http_500", "http_502", "http_503", "http_504", "http_529"}
                    if not retryable or attempt == args.api_retries:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                latency = (time.monotonic() - request_start) * 1000
                latencies.append(latency)
                input_tokens += response["usage"].get("input_tokens", 0)
                output_tokens += response["usage"].get("output_tokens", 0)
                record["input_tokens"] += response["usage"].get("input_tokens", 0)
                record["output_tokens"] += response["usage"].get("output_tokens", 0)
                emit("model_response", episode=episode, step=step, batch_id=batch_id,
                     request_id=request_id, latency_ms=latency, **response)
                return response
    try:
        if args.planner == "codex":
            planner = MapPlanner(output / "planner", emit, args.planner_model,
                                 args.planner_effort, args.planner_timeout, args.codex_path)
        for episode in range(1, args.episodes + 1):
            episode_dir = output / f"episode-{episode:03d}"
            episode_dir.mkdir()
            episode_start = time.monotonic()
            record = {"episode": episode, "policy": args.policy, "status": "starting",
                      "steps": 0, "requests": 0, "reward": 0.0, "input_tokens": 0, "output_tokens": 0}
            ready = False
            try:
                env.reset(opponent=opponent_for(env, args.opponent), return_features=False)
                ready = True
                record["opponent_script"] = type(env.dts_script).__module__
                record["opponent_class"] = type(env.dts_script).__name__
                if hasattr(env.dts_script, "destination"):
                    record["opponent_destination"] = list(env.dts_script.destination)
                record["initial"] = battle_state(env)
                record["initial_game_loop"] = env._obs.observation.game_loop
                if args.episode_limit:
                    env.episode_limit = args.episode_limit
                record["episode_limit"] = env.episode_limit
                env.scenario_brief = build_scenario_brief(env, args.opponent)
                record["scenario_brief_sha256"] = env.scenario_brief["sha256"]
                write_json(episode_dir / "scenario_brief.json", env.scenario_brief)
                emit("scenario_brief", episode=episode, brief=env.scenario_brief)
                data = env.micro_data
                ping = env._controllers[0].ping()
                record["game_version"] = ping.game_version
                record["base_build"] = ping.base_build
                emit("episode_start", **record)
                previous_health, previous_actions, previous_shields = {}, {}, {}
                previous_result = None
                terminated = False
                while not terminated:
                    payload = build_request(env, data, args.model, episode, previous_health,
                                            previous_actions, previous_shields, previous_result)
                    observation_loop = payload["state"]["game_loop"]
                    if planner is not None:
                        if active_plan is None:
                            print(f"Planning {args.map} with {args.planner_model} ({args.planner_effort})", flush=True)
                            active_plan = await planner.get_plan(payload, env)
                            fresh, abilities = env._controllers[0].observe()
                            if fresh.observation.game_loop != observation_loop:
                                raise RuntimeError("SC2 advanced while waiting for Astra in fixed-step mode.")
                            env._obs, env._ability = fresh, abilities
                            planner_fixed_step_checks += 1
                            emit("planner_clock_check", before_game_loop=observation_loop,
                                 after_game_loop=fresh.observation.game_loop, unchanged=True)
                            print(f"Plan accepted for {args.map}; reused across {args.episodes} episodes", flush=True)
                        payload = attach_plan(payload, active_plan)
                        record["plan_sha256"] = active_plan["plan_sha256"]
                    if not payload["questions"]:
                        raise RuntimeError("No live units without an environment terminal result.")
                    emit("observation", episode=episode, step=record["steps"], payload=payload)
                    if client is not None:
                        batches = batch_requests(payload)
                        request_start = time.monotonic()
                        responses = await asyncio.gather(*(fetch_batch(batch, episode, record["steps"], i, record)
                                                          for i, batch in enumerate(batches)), return_exceptions=True)
                        errors = [r for r in responses if isinstance(r, BaseException)]
                        if errors:
                            raise errors[0]
                        answers = {actor: answer for response in responses for actor, answer in response["answers"].items()}
                        actions = decode_actions(payload, answers, env.n_agents)
                        usage = {key: sum(r["usage"].get(key, 0) for r in responses)
                                 for key in ("input_tokens", "output_tokens")}
                        emit("decision", episode=episode, step=record["steps"], batch_count=len(batches),
                             latency_ms=(time.monotonic() - request_start) * 1000, actions=actions,
                             model=responses[0]["model"], answers=answers, usage=usage)
                        # Read the actual SC2 clock; don't infer paused time from a cached snapshot.
                        fresh, abilities = env._controllers[0].observe()
                        if fresh.observation.game_loop != observation_loop:
                            raise RuntimeError("SC2 advanced while waiting for Jev in fixed-step mode.")
                        env._obs, env._ability = fresh, abilities
                        fixed_step_checks += 1
                    else:
                        actions = nearest_actions(payload, env.n_agents)
                        emit("decision", episode=episode, step=record["steps"], actions=actions, policy="nearest")
                    for i, action in enumerate(actions):
                        if not env.get_avail_agent_actions(i)[action]:
                            raise RuntimeError(f"Action for u{i} failed execution-time validation.")
                    previous_health = {u.tag: u.health for u in env.agents.values()}
                    previous_shields = {u.tag: u.shield for u in env.agents.values()}
                    previous_actions = {f"u{i}": str(a) for i, a in enumerate(actions)}
                    reward, terminated, info = env.step(actions)
                    if info.get("backend_error"):
                        raise RuntimeError(info["backend_error"])
                    record["steps"] += 1
                    record["reward"] += reward
                    new_loop = env._obs.observation.game_loop
                    if not terminated and new_loop - observation_loop != args.step_mul:
                        raise RuntimeError("Unexpected simulation step size.")
                    raw_results = list(env.last_action_results[0].result)
                    previous_result = action_feedback(env, observation_loop)
                    failed = [value for value in raw_results if value != 1]
                    action_errors += len(failed)
                    observation_errors = [{"unit_tag": str(e.unit_tag), "ability_id": e.ability_id,
                                           "result": e.result} for e in env._obs.action_errors]
                    action_errors += len(observation_errors)
                    emit("step", episode=episode, step=record["steps"],
                         observation_game_loop=observation_loop, game_loop=new_loop,
                         actions=actions, action_results=raw_results,
                         action_feedback=previous_result,
                         action_errors=observation_errors, reward=reward, info=info,
                         evaluation=battle_state(env))
                    if record["steps"] % 10 == 0 or terminated:
                        health = battle_state(env)
                        print(f"Episode {episode}: step={record['steps']} allies={health['allies_alive']} "
                              f"enemies={health['enemies_alive']} HP={health['allied_health']}/{health['enemy_health']}", flush=True)
                    if terminated:
                        record["status"] = ("win" if info.get("battle_won") else
                                            "timeout" if info.get("episode_limit") else
                                            "draw" if info.get("dead_allies") == env.n_agents
                                            and info.get("dead_enemies") == env.n_enemies else "loss")
                        record["final_info"] = info
            except (Exception, KeyboardInterrupt) as exc:
                record["status"] = "error" if not isinstance(exc, KeyboardInterrupt) else "interrupted"
                # Client exceptions are deliberately sanitized. Do not dump HTTP objects.
                record["error"] = str(exc) if isinstance(exc, (JevError, RuntimeError, ValueError)) else type(exc).__name__
                failure = record["error"]
                emit("error", episode=episode, error=record["error"], exception_type=type(exc).__name__)
                raise
            finally:
                record["wall_seconds"] = round(time.monotonic() - episode_start, 3)
                record["estimated_api_cost_usd"] = round(record["input_tokens"] * 0.042 / 1_000_000, 9)
                if ready:
                    record["final"] = battle_state(env)
                    record["final_game_loop"] = env._obs.observation.game_loop
                    record["game_seconds"] = round((record["final_game_loop"] - record["initial_game_loop"]) / 22.4, 3)
                    try:
                        replay = episode_dir / "game.SC2Replay"
                        replay.write_bytes(env._controllers[0].save_replay())
                        record["replay"] = str(replay)
                    except Exception as exc:
                        record["replay_error"] = type(exc).__name__
                episodes.append(record)
                write_json(episode_dir / "summary.json", record)
                emit("episode_end", **record)
                print(f"Episode {episode}: {record['status']} ({record['steps']} steps)", flush=True)
    finally:
        try:
            env.close()
        finally:
            if client is not None:
                await client.close()
            if planner is not None:
                await planner.close()
            completed = [e for e in episodes if e["status"] in {"win", "loss", "draw", "timeout"}]
            summary = {
                **settings, "output_dir": str(output), "episodes": episodes,
                "completed_episodes": len(completed),
                "wins": sum(e["status"] == "win" for e in completed),
                "requests": requests, "successful_responses": len(latencies), "api_errors": api_errors,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "estimated_api_cost_usd": round(input_tokens * 0.042 / 1_000_000, 9),
                "price_basis": {"input_usd_per_million": 0.042, "output_usd_per_million": 0,
                                "checked_on": "2026-09-21", "source": "https://docs.typesafe.ai/models"},
                "sc2_action_errors": action_errors, "fixed_step_checks": fixed_step_checks,
                "planner_fixed_step_checks": planner_fixed_step_checks,
                "planner_result": planner.stats if planner is not None else None,
                "latency_median_ms": statistics.median(latencies) if latencies else None,
                "latency_max_ms": max(latencies) if latencies else None,
                "wall_seconds": round(time.monotonic() - started, 3), "failure": failure,
                "note": "Fixed-step, centralized team-visible micro; model wait time does not advance SC2. Small-sample smoke test, not a win-rate benchmark.",
            }
            write_json(output / "summary.json", summary)
            log_file.close()
            print(f"Summary: {output / 'summary.json'}", flush=True)
    return output


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", choices=SUPPORTED_MAPS, default="3m")
    parser.add_argument("--policy", choices=["jev", "nearest"], default="jev")
    parser.add_argument("--opponent", choices=["base", "nearest", "weakest", "mixed"], default="base")
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--step-mul", type=int, default=8)
    parser.add_argument("--episode-limit", type=int, help="Defaults to the selected SMAC-Hard map's step limit")
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--request-timeout", type=positive, default=5.0)
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--batch-concurrency", type=int, default=4)
    parser.add_argument("--api-retries", type=int, default=2)
    parser.add_argument("--sc2-path", type=Path, default=Path(os.environ.get("SC2PATH", r"C:\game\StarCraft II")))
    parser.add_argument("--config-file", type=Path, default=REPO.parent / "config.md")
    parser.add_argument("--output-dir", type=Path)
    add_planner_arguments(parser)
    args = parser.parse_args()
    if min(args.episodes, args.step_mul, args.max_requests, args.batch_concurrency) < 1 or args.api_retries < 0 or (args.episode_limit is not None and args.episode_limit < 1):
        parser.error("episodes, step-mul, max-requests and episode-limit must be positive")
    if args.planner != "none" and args.policy != "jev":
        parser.error("The Astra planner requires --policy jev")
    try:
        api_key = load_api_key(args.config_file) if args.policy == "jev" else None
        asyncio.run(run(args, api_key))
    except (JevError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Run stopped: {exc}\n")


if __name__ == "__main__":
    main()
