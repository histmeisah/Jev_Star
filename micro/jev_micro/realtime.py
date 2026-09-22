"""Run JEV while SC2 and the scripted opponent continue in real time."""

import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import time

import numpy as np
from absl import flags, logging
from s2clientprotocol import sc2api_pb2 as sc

from .catalog import SUPPORTED_MAPS, map_source
from .client import JevClient, JevError, load_api_key
from .environment import JevMicroEnv, is_medivac, opponent_for
from .policy import build_request, batch_requests, decode_actions, nearest_actions
from .run import REPO, battle_state, installation_info, install_map, positive, write_json
from .windows_session import active_display


SCHEMA_VERSION = 4
LOOPS_PER_SECOND = 22.4


def request_snapshot(env, model, episode, limit_loop, previous_health,
                     previous_actions, previous_shields):
    payload = build_request(env, env.micro_data, model, episode, previous_health,
                            previous_actions, previous_shields)
    state = payload["state"]
    state["schema_version"] = SCHEMA_VERSION
    state.pop("step_mul")
    state.pop("steps_remaining")
    state.update(objective="Eliminate all opposing units before game_loop_limit.",
                 realtime=True, game_loop_limit=limit_loop,
                 game_loops_remaining=max(0, limit_loop - state["game_loop"]),
                 timing="SC2 continues during inference. One observation is shared by all unit questions. "
                        "Commands execute when the response arrives; positions and health may have changed.")
    for question in payload["questions"].values():
        question["instructions"] = question["instructions"].replace(
            "Each surviving ally receives one action at this same game loop.",
            "Each surviving ally chooses one action from this observation. "
            "The battle continues during inference; chosen commands execute when the response arrives.")
    return payload


def snapshot_commands(env, payload):
    """Freeze exactly the tags and movement endpoints offered to the model."""
    return {actor: {action: copy.deepcopy(env.get_agent_action(int(actor[1:]), int(action)))
                    for action in question["criteria"]}
            for actor, question in payload["questions"].items()}


def executable_choices(env, payload, answers, frozen):
    """Drop stale invalid choices without selecting a replacement action."""
    decode_actions(payload, answers, env.n_agents)
    visible = {unit.tag for unit in env._obs.observation.raw_data.units
               if unit.owner == 2 and unit.display_type == 1 and unit.health > 0}
    commands, applied, dropped = [], {}, []
    for actor, answer in answers.items():
        index, action = int(actor[1:]), int(answer["choice"])
        unit = env.agents[index]
        reason = None
        if unit.health <= 0 or str(unit.tag) != payload["state"]["allies"][actor]["tag"]:
            reason = "actor_dead_or_replaced"
        elif action >= 6:
            if not is_medivac(unit, env.micro_data) and env.enemies[action - 6].tag not in visible:
                reason = "target_no_longer_visible"
            elif not env.get_avail_agent_actions(index)[action]:
                reason = "target_no_longer_available"
        elif action in (2, 3, 4, 5):
            target = frozen[actor][str(action)].action_raw.unit_command.target_world_space_pos
            if not env.check_bounds(target.x, target.y):
                reason = "movement_endpoint_outside_map"
        if reason:
            dropped.append({"actor": actor, "choice": str(action), "reason": reason})
            continue
        command = frozen[actor][str(action)]
        if command is not None:
            commands.append(command)
        applied[actor] = str(action)
    return commands, applied, dropped


def distribution(values):
    if not values:
        return None
    return {"count": len(values), "median": statistics.median(values),
            "mean": statistics.mean(values), "p95": float(np.percentile(values, 95)),
            "min": min(values), "max": max(values)}


async def run(args, api_key):
    if not flags.FLAGS.is_parsed():
        flags.FLAGS(["jev-realtime", "--sc2_timeout=45"])
    logging.set_verbosity(logging.WARNING)
    os.environ["SC2PATH"] = str(args.sc2_path)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    map_sha = install_map(args.sc2_path, args.map)
    settings = {key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items() if key != "config_file"}
    settings.update(realtime=True, schema_version=SCHEMA_VERSION,
                    visibility="team_visible", use_ability=False, map_sha256=map_sha,
                    map_source=str(map_source(args.map).relative_to(REPO)),
                    inference_scheduling="one in-flight team decision; next fresh observation after execution",
                    movement_semantics="snapshot endpoint retained during inference",
                    stale_action_policy="drop invalid choices and retain current engine orders",
                    **installation_info(args.sc2_path))
    settings["source_sha256"] = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
        for name in ("jev_micro/realtime.py", "jev_micro/client.py", "jev_micro/policy.py",
                     "jev_micro/environment.py", "smac_hard/env/starcraft2/starcraft2.py")}
    write_json(output / "run.json", settings)
    stream = (output / "events.jsonl").open("w", encoding="utf-8", buffering=1)
    origin = time.monotonic()

    def emit(event, **fields):
        stream.write(json.dumps({"event": event, "wall_since_run_start": time.monotonic() - origin,
                                 **fields}, ensure_ascii=False, allow_nan=False) + "\n")

    client = JevClient(api_key, args.request_timeout) if args.policy == "jev" else None
    semaphore = asyncio.Semaphore(args.batch_concurrency)
    request_number = 0
    all_latencies, records = [], []
    failure = None

    async def infer(payload, record, decision_id):
        nonlocal request_number
        if client is None:
            await asyncio.sleep(args.simulated_latency_ms / 1000)
            actions = nearest_actions(payload, len(payload["state"]["allies"]))
            return {actor: {"choice": str(actions[int(actor[1:])])}
                    for actor in payload["questions"]}
        batches = batch_requests(payload)
        for batch in batches:
            if "batch_context" in batch["state"]:
                batch["state"]["batch_context"] = "Team overview and full features for batch_actors. All batches use one observation; SC2 continues in real time."

        async def fetch(batch, batch_id):
            nonlocal request_number
            async with semaphore:
                if request_number >= args.max_requests:
                    raise JevError("request_budget_exhausted")
                request_number += 1
                request_id = request_number
                record["requests"] += 1
                emit("model_request", episode=record["episode"], step=decision_id,
                     request_id=request_id, batch_id=batch_id, payload=batch)
                started = time.monotonic()
                try:
                    response = await client.choose(batch)
                except JevError as exc:
                    record["api_errors"] += 1
                    emit("api_error", episode=record["episode"], step=decision_id,
                         request_id=request_id, error=str(exc))
                    raise
                latency = (time.monotonic() - started) * 1000
                all_latencies.append(latency)
                record["input_tokens"] += response["usage"].get("input_tokens", 0)
                record["output_tokens"] += response["usage"].get("output_tokens", 0)
                emit("model_response", episode=record["episode"], step=decision_id,
                     request_id=request_id, batch_id=batch_id, latency_ms=latency, **response)
                return response
        responses = await asyncio.gather(*(fetch(batch, i) for i, batch in enumerate(batches)))
        return {actor: answer for response in responses for actor, answer in response["answers"].items()}

    try:
        with active_display():
            for episode in range(1, args.episodes + 1):
                directory = output / f"episode-{episode:03d}"
                directory.mkdir()
                seed = args.seed + episode - 1
                random.seed(seed)
                np.random.seed(seed)
                env = JevMicroEnv(map_name=args.map, step_mul=8, seed=seed,
                                  use_ability=False, continuing_episode=True, realtime=True)
                record = dict(episode=episode, seed=seed, policy=args.policy, status="starting",
                              steps=0, decisions_completed=0, decisions_applied=0,
                              units_applied=0, units_dropped=0, decisions_after_end=0,
                              requests=0, input_tokens=0, output_tokens=0, api_errors=0,
                              sc2_action_errors=0, opponent_ticks=0, opponent_ticks_during_inference=0,
                              missed_opponent_ticks=0)
                records.append(record)
                pending = None
                replay_saved = False
                active_start = None
                ages, inference_ms, cadence, opponent_lateness = [], [], [], []
                try:
                    # Fresh SC2 game for each episode: no unit-reset races in realtime.
                    await asyncio.to_thread(env.reset, opponent_for(env, args.opponent), False)
                    active_start = time.monotonic()
                    initial_loop = env._obs.observation.game_loop
                    limit_loop = initial_loop + (args.max_game_loops or env.episode_limit * 8)
                    record.update(initial=battle_state(env), initial_game_loop=initial_loop,
                                  game_loop_limit=limit_loop, opponent_script=type(env.dts_script).__module__)
                    emit("episode_start", **record)
                    previous_health, previous_shields, previous_actions = {}, {}, {}
                    last_sample_loop, last_opponent_slot, last_applied_loop = -1, -1, -1
                    last_decision_start = None
                    last_progress = active_start
                    while True:
                        observations = await asyncio.to_thread(env.parallel.run,
                            [controller.observe for controller in env._controllers])
                        (env._obs, env._ability), (env.blue_obs, env.blue_ability) = observations
                        code = env.update_units()
                        loop = env._obs.observation.game_loop
                        blue_loop = env.blue_obs.observation.game_loop
                        now = time.monotonic()
                        env._episode_steps = max(0, loop - initial_loop) // 8
                        if loop != last_sample_loop:
                            record["steps"] += 1
                            emit("step", episode=episode, step=record["steps"], game_loop=loop,
                                 evaluation=battle_state(env),
                                 evaluation_observation_loops={"allies": loop, "enemies": blue_loop},
                                 model_inflight=pending is not None, battle_wall_seconds=now - active_start)
                            last_sample_loop = loop
                        for side, observation in enumerate((env._obs, env.blue_obs)):
                            for action in observation.actions:
                                if action.action_raw.HasField("unit_command"):
                                    command = action.action_raw.unit_command
                                    emit("action_observed", episode=episode, side=side,
                                         game_loop=action.game_loop, observation_game_loop=observation.observation.game_loop,
                                         ability_id=command.ability_id, unit_tags=[str(tag) for tag in command.unit_tags],
                                         target_tag=str(command.target_unit_tag) if command.HasField("target_unit_tag") else None,
                                         target_position=[command.target_world_space_pos.x, command.target_world_space_pos.y]
                                         if command.HasField("target_world_space_pos") else None)
                            errors = [{"unit_tag": str(error.unit_tag), "ability_id": error.ability_id,
                                       "result": error.result} for error in observation.action_errors]
                            if errors:
                                record["sc2_action_errors"] += len(errors)
                                emit("sc2_action_errors", episode=episode, side=side,
                                     game_loop=observation.observation.game_loop, errors=errors)
                        if code is not None or loop >= limit_loop:
                            record["status"] = {1: "win", -1: "loss", 0: "draw"}.get(code, "timeout")
                            record.update(final=battle_state(env), final_game_loop=loop,
                                          battle_wall_seconds=now - active_start)
                            replay_path = directory / "game.SC2Replay"
                            replay_path.write_bytes(await asyncio.to_thread(env._controllers[0].save_replay))
                            record["replay"] = str(replay_path)
                            replay_saved = True
                            if pending is not None:
                                answers = await pending["task"]
                                record["decisions_after_end"] += 1
                                emit("decision_discarded", episode=episode, step=pending["id"],
                                     reason="battle_ended_before_execution", game_loop=loop, answers=answers)
                                pending = None
                            break
                        slot = max(0, blue_loop - initial_loop) // args.opponent_step_mul
                        if slot > last_opponent_slot:
                            record["missed_opponent_ticks"] += max(0, slot - last_opponent_slot - 1)
                            commands = env.dts_script.script(env.enemies, env.agents, env.blue_ability,
                                env.get_fog_visibility_matrix(), slot)
                            commands = [command for command in commands if command is not None]
                            response = await asyncio.to_thread(env._controllers[1].actions,
                                                              sc.RequestAction(actions=commands))
                            lateness = blue_loop - (initial_loop + slot * args.opponent_step_mul)
                            opponent_lateness.append(lateness)
                            record["opponent_ticks"] += 1
                            record["opponent_ticks_during_inference"] += int(pending is not None)
                            record["sc2_action_errors"] += sum(value != 1 for value in response.result)
                            emit("opponent_action", episode=episode, game_loop=blue_loop,
                                 scheduled_game_loop=initial_loop + slot * args.opponent_step_mul,
                                 command_count=len(commands), action_results=list(response.result),
                                 model_inflight=pending is not None)
                            last_opponent_slot = slot
                        if pending is not None and pending["task"].done():
                            answers = pending["task"].result()
                            commands, applied, dropped = executable_choices(
                                env, pending["payload"], answers, pending["commands"])
                            age = loop - pending["payload"]["state"]["game_loop"]
                            elapsed_ms = (time.monotonic() - pending["started"]) * 1000
                            ages.append(age)
                            inference_ms.append(elapsed_ms)
                            response = await asyncio.to_thread(env._controllers[0].actions,
                                                              sc.RequestAction(actions=commands))
                            record["decisions_completed"] += 1
                            record["decisions_applied"] += bool(applied)
                            record["units_applied"] += len(applied)
                            record["units_dropped"] += len(dropped)
                            record["sc2_action_errors"] += sum(value != 1 for value in response.result)
                            emit("decision", episode=episode, step=pending["id"], game_loop=loop,
                                 observation_game_loop=pending["payload"]["state"]["game_loop"],
                                 state_age_loops=age, observation_to_dispatch_ms=elapsed_ms,
                                 answers=answers, applied=applied, dropped=dropped,
                                 action_results=list(response.result))
                            previous_actions.update(applied)
                            last_applied_loop = loop
                            pending = None
                        if pending is None and loop > last_applied_loop:
                            payload = request_snapshot(env, args.model, episode, limit_loop,
                                previous_health, previous_actions, previous_shields)
                            if not payload["questions"]:
                                raise RuntimeError("No living actors before a terminal outcome")
                            identifier = record["decisions_completed"]
                            started = time.monotonic()
                            if last_decision_start is not None:
                                cadence.append((started - last_decision_start) * 1000)
                            last_decision_start = started
                            emit("observation", episode=episode, step=identifier, payload=payload)
                            frozen = snapshot_commands(env, payload)
                            pending = {"id": identifier, "payload": payload, "commands": frozen,
                                       "started": started, "task": asyncio.create_task(infer(payload, record, identifier))}
                            previous_health = {unit.tag: unit.health for unit in env.agents.values()}
                            previous_shields = {unit.tag: unit.shield for unit in env.agents.values()}
                        if time.monotonic() - last_progress > 5:
                            print(f"Episode {episode}: loop={loop} decisions={record['decisions_completed']} "
                                  f"alive={sum(u.health > 0 for u in env.agents.values())}/"
                                  f"{sum(u.health > 0 for u in env.enemies.values())}", flush=True)
                            last_progress = time.monotonic()
                        await asyncio.sleep(args.poll_seconds)
                except BaseException as exc:
                    record["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "error"
                    record["error"] = str(exc) if isinstance(exc, (JevError, ValueError, RuntimeError)) else type(exc).__name__
                    emit("error", episode=episode, error=record["error"])
                    failure = record["error"]
                    raise
                finally:
                    if pending is not None:
                        pending["task"].cancel()
                        await asyncio.gather(pending["task"], return_exceptions=True)
                    if active_start is not None:
                        record.setdefault("final", battle_state(env))
                        record.setdefault("final_game_loop", env._obs.observation.game_loop)
                        record.setdefault("battle_wall_seconds", time.monotonic() - active_start)
                        record["game_seconds"] = (record["final_game_loop"] - record["initial_game_loop"]) / LOOPS_PER_SECOND
                        record["measured_game_loops_per_wall_second"] = (
                            record["final_game_loop"] - record["initial_game_loop"]) / record["battle_wall_seconds"]
                        record["decision_hz_wall"] = record["decisions_completed"] / record["battle_wall_seconds"]
                        if not replay_saved:
                            try:
                                replay_path = directory / "game.SC2Replay"
                                replay_path.write_bytes(await asyncio.to_thread(env._controllers[0].save_replay))
                                record["replay"] = str(replay_path)
                            except Exception as exc:
                                record["replay_error"] = type(exc).__name__
                    record.update(state_age_loops=distribution(ages), observation_to_dispatch_ms=distribution(inference_ms),
                                  decision_start_intervals_ms=distribution(cadence),
                                  opponent_lateness_loops=distribution(opponent_lateness),
                                  decisions_with_game_progress=sum(age > 0 for age in ages),
                                  estimated_api_cost_usd=record["input_tokens"] * 0.042 / 1_000_000)
                    write_json(directory / "summary.json", record)
                    emit("episode_end", **record)
                    await asyncio.to_thread(env.close)
                    print(f"Episode {episode}: {record['status']}; decisions={record['decisions_completed']}; "
                          f"dropped_units={record['units_dropped']}", flush=True)
    finally:
        if client is not None:
            await client.close()
        summary = {**settings, "episodes": records, "requests": request_number,
                   "wins": sum(record["status"] == "win" for record in records),
                   "api_errors": sum(record["api_errors"] for record in records),
                   "input_tokens": sum(record["input_tokens"] for record in records),
                   "output_tokens": sum(record["output_tokens"] for record in records),
                   "request_latency_ms": distribution(all_latencies), "failure": failure,
                   "note": "Realtime smoke test with response-driven JEV control and independently scheduled opponent. "
                           "Small samples; prompt timing and fresh-per-episode seeds differ from historical fixed-step runs."}
        summary["estimated_api_cost_usd"] = summary["input_tokens"] * 0.042 / 1_000_000
        write_json(output / "summary.json", summary)
        stream.close()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", choices=SUPPORTED_MAPS, default="3m")
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--policy", choices=("jev", "nearest"), default="jev")
    parser.add_argument("--opponent", choices=("base", "nearest", "weakest"), default="base")
    parser.add_argument("--opponent-step-mul", type=int, default=8)
    parser.add_argument("--poll-seconds", type=positive, default=0.01)
    parser.add_argument("--max-game-loops", type=int)
    parser.add_argument("--simulated-latency-ms", type=float, default=0)
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--request-timeout", type=positive, default=5)
    parser.add_argument("--max-requests", type=int, default=500)
    parser.add_argument("--batch-concurrency", type=int, default=4)
    parser.add_argument("--sc2-path", type=Path, default=Path(r"C:\game\StarCraft II"))
    parser.add_argument("--config-file", type=Path, default=REPO.parent / "config.md")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if min(args.episodes, args.opponent_step_mul, args.max_requests, args.batch_concurrency) < 1:
        parser.error("Counts and intervals must be positive")
    if args.max_game_loops is not None and args.max_game_loops < 1:
        parser.error("max-game-loops must be positive")
    if not math.isfinite(args.simulated_latency_ms) or args.simulated_latency_ms < 0:
        parser.error("simulated-latency-ms must be finite and nonnegative")
    if args.policy == "jev" and args.simulated_latency_ms:
        parser.error("simulated-latency-ms is only for the nearest smoke test")
    key = load_api_key(args.config_file) if args.policy == "jev" else None
    asyncio.run(run(args, key))


if __name__ == "__main__":
    main()
