"""Run and audit a preregistered series of uniform-random macro games."""

import argparse
import csv
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .agent.random_macro import RandomMacroClient
from .run_jev import positive_float
from .utils.run_logging import atomic_json


DIFFICULTIES = {name: index + 1 for index, name in enumerate((
    "VeryEasy", "Easy", "Medium", "MediumHard", "Hard", "Harder", "VeryHard",
    "CheatVision", "CheatMoney", "CheatInsane"))}
SOURCE_DIR = Path(__file__).resolve().parent
REPO = SOURCE_DIR.parents[1]
SOURCES = (
    "agent/astra_planner.py", "agent/jev_agent.py", "agent/strategic_policy.py", "agent/macro_contract.py",
    "agent/random_macro.py", "env/bot/Protoss_bot.py", "env/bot/jev_protoss_bot.py",
    "env/bot/hierarchical_protoss_bot.py", "env/bot/macro_execution.py", "env/bot/macro_navigation.py",
    "run_jev.py", "run_random_ablation.py", "utils/run_logging.py", "utils/sc2_runtime.py", "utils/action_info.py",
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wilson_interval(wins, total):
    if not total:
        return None
    z = 1.959963984540054
    rate, denominator = wins / total, 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return [max(0, center - radius), min(1, center + radius)]


def audit_trial(directory, protocol, seed, policy_seed):
    """Verify actual random choices and replay outcome; never infer a loss from a crash."""
    import mpyq
    import sc2reader

    settings = read_json(directory / "run.json")
    summary = read_json(directory / "summary.json")
    events_path = directory / "events.jsonl"
    events_hash = sha256(events_path)
    checks = {
        "runtime_completed": summary.get("status") == "completed",
        "pure_random_configuration": settings.get("policy") == "random" and settings.get("planner") == "none"
            and settings.get("model") == RandomMacroClient.model and settings.get("model_api_calls") is False,
        "seeds_match": settings.get("seed") == seed and settings.get("policy_seed") == policy_seed,
        "experiment_configuration_matches": all(settings.get(key) == protocol[key] for key in (
            "map", "difficulty", "opponent_race", "game_time_limit", "decision_interval", "max_requests")),
        "timebase_matches": settings.get("realtime") == protocol["realtime"]
            and settings.get("decision_timebase") == protocol["decision_timebase"],
    }
    fingerprints = read_json(directory / "source-fingerprints.json")
    frozen = protocol["source_fingerprints"]
    checks["frozen_runtime"] = bool(fingerprints) and all(frozen.get(name) == digest for name, digest in fingerprints.items())
    rng = random.Random(policy_seed)
    requests, actions, events = {}, Counter(), Counter()
    random_matches = legal = uniform = no_plans = cadence = True
    response_count, last_start = 0, None
    map_hash = None
    with events_path.open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            kind = event["event"]
            events[kind] += 1
            if kind == "environment":
                map_hash = event["map_sha256"]
            elif kind == "request":
                requests[event["request_id"]] = event
                no_plans &= event.get("plan_id") is None and not event["payload"]["state"].get("strategic_plan")
                start = event["wall_seconds"] if protocol["realtime"] else event["game_seconds"]
                if last_start is not None:
                    cadence &= start - last_start >= protocol["decision_interval"] - .01
                last_start = start
            elif kind == "response":
                request = requests[event["request_id"]]
                choices = request["payload"]["questions"]["next_macro_action"]["criteria"]
                answer = event["answer"]
                random_matches &= answer["choice"] == rng.choice(sorted(choices, key=int))
                legal &= answer["choice"] in choices
                uniform &= set(answer["probabilities"]) == set(choices) and all(
                    math.isclose(p, 1 / len(choices), abs_tol=1e-12) for p in answer["probabilities"].values())
                no_plans &= event.get("model") == RandomMacroClient.model and event.get("usage") == {
                    "input_tokens": 0, "output_tokens": 0}
                response_count += 1
            elif kind == "action":
                actions[str(event["action_id"])] += 1
    checks.update(
        random_stream_reproduced=random_matches and response_count > 0,
        choices_legal=legal, probabilities_uniform=uniform,
        no_model_or_planner_responses=no_plans and events["planner_request"] == 0 and events["plan_accepted"] == 0,
        request_cadence_respected=cadence,
        decision_budget_not_exhausted=len(requests) < protocol["max_requests"],
        map_hash_matches=map_hash == protocol["map_sha256"],
    )
    replay_path = directory / "game.SC2Replay"
    metadata = json.loads(mpyq.MPQArchive(str(replay_path)).read_file("replay.gamemetadata.json"))
    replay = sc2reader.load_replay(str(replay_path), load_level=0)
    for name in ("replay.initData", "replay.details"):
        replay._read_data(name, replay._get_reader(name))
    slots = replay.raw_data["replay.initData"]["lobby_state"]["slots"]
    opponents = [slot for slot in slots if slot["control"] == 3]
    player = next(p for p in metadata["Players"] if p["PlayerID"] == 1)
    opponent = next(p for p in metadata["Players"] if p["PlayerID"] == 2)
    expected = {"Victory": "Win", "Defeat": "Loss", "Tie": "Undecided"}.get(summary.get("result"))
    checks.update(
        one_computer_opponent=len(opponents) == 1,
        difficulty_matches=len(opponents) == 1 and opponents[0]["difficulty"] == DIFFICULTIES[protocol["difficulty"]],
        random_build_matches=len(opponents) == 1 and opponents[0]["ai_build"] == 1,
        no_handicap=all(s["handicap"] == 100 for s in slots),
        replay_map_matches=metadata["Title"] == protocol["map"],
        replay_races_match=player["AssignedRace"] == "Prot" and opponent["AssignedRace"] == protocol["opponent_race"],
        replay_result_matches=expected is not None and player["Result"] == expected,
        replay_end_matches_last_observation=0 <= replay.frames - summary["game_loop"] <= 23,
        raw_events_unchanged=sha256(events_path) == events_hash,
    )
    result = {"run": directory.name, "seed": seed, "policy_seed": policy_seed,
              "result": summary.get("result"), "runtime_status": summary.get("status"),
              "game_seconds": replay.frames / 22.4, "wall_seconds": summary["wall_seconds"], "decisions": response_count,
              "actions": dict(actions), "checks": checks, "valid": all(checks.values()),
              "replay_result": player["Result"], "sc2_version": metadata["GameVersion"],
              "player_names": [p["name"] for p in replay.raw_data["replay.details"]["players"]],
              "events_sha256": events_hash, "replay_sha256": sha256(replay_path),
              "model_api_calls": 0, "model_api_cost_usd": 0}
    atomic_json(directory / "random-audit.json", result)
    return result


def summarize(trials, planned):
    valid = [trial for trial in trials if trial.get("valid")]
    counts = Counter(trial["result"] for trial in valid)
    total, wins = len(valid), counts["Victory"]
    return {"planned_games": planned, "verified_games": total, "wins": wins,
            "losses": counts["Defeat"], "ties": counts["Tie"],
            "invalid_attempts": sum(not t.get("valid") for t in trials),
            "win_rate": wins / total if total else None,
            "win_rate_95pct_wilson": wilson_interval(wins, total),
            "denominator": "Replay-verified completed games, including ties; invalid attempts excluded.",
            "model_api_calls": 0, "model_api_cost_usd": 0}


def write_results(directory, protocol, trials, status):
    aggregate = summarize(trials, len(protocol["seeds"]))
    result = {"status": status, "updated_at_utc": datetime.now(timezone.utc).isoformat(),
              "protocol": protocol, "aggregate": aggregate, "trials": trials}
    atomic_json(directory / "results.json", result)
    with (directory / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("run", "seed", "policy_seed", "valid", "result", "game_seconds", "decisions", "error"), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trials)
    rate = "尚无有效结果" if aggregate["win_rate"] is None else f"{100 * aggregate['win_rate']:.1f}%"
    mode = "实时模式" if protocol["realtime"] else "非实时加速模式（realtime=False）"
    timebase = "墙钟秒" if protocol["realtime"] else "游戏秒"
    lines = ["# 宏观决策：均匀随机消融实验", "",
             f"状态：{status}。有效对局 {aggregate['verified_games']}/{aggregate['planned_games']}；"
             f"{aggregate['wins']} 胜、{aggregate['losses']} 负、{aggregate['ties']} 平。", "",
             f"样本胜率：**{rate}**。异常尝试 {aggregate['invalid_attempts']} 次，单独记录。", "",
             f"设置：{protocol['map']}，Protoss 对 {protocol['opponent_race']}，API 难度 {protocol['difficulty']}，"
             f"{mode}，并发 {protocol['parallel']} 局，每局最多 {protocol['game_time_limit']:g} 游戏秒，种子 {protocol['seeds']}。", "",
             "关闭 Astra/Jev，等概率抽取当前合法宏观动作，包括等待；保留相同的动作筛选、工人分配、折跃门转换、"
             f"侦察任务和军队姿态执行。动作请求间隔至少一{timebase}，不加入模型推理延迟。零模型 API 调用。", "",
             "这是整个模型决策层被替换后的基线；动作筛选仍包含现有策略上限及人口预测规则。"
             "它不能单独区分 Astra 和 Jev 各自的贡献。旧版两局获胜记录不作为同版本配对胜率。", "",
             "| 运行 | 游戏种子 | 策略种子 | 结果 | 游戏时间 | 验证 |", "|---|---:|---:|---|---:|---|",]
    for trial in trials:
        seconds = trial.get("game_seconds")
        duration = "—" if seconds is None else f"{int(seconds) // 60}:{int(seconds) % 60:02d}"
        link = f"[{trial['run']}]({trial['run']}/random-audit.json)" if trial.get("checks") else trial["run"]
        lines.append(f"| {link} | {trial['seed']} | {trial['policy_seed']} | {trial.get('result', '异常')} | {duration} | {'通过' if trial.get('valid') else '未通过'} |")
    lines += ["", "胜率分母为回放核验通过的胜、负、平局总数；技术失败不记为败局。"
              "每局保留 run.json、events.jsonl、summary.json、game.SC2Replay 和 random-audit.json。", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--difficulty", choices=list(DIFFICULTIES), default="VeryHard")
    parser.add_argument("--map", default="Altitude LE")
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--start-seed", type=int, default=1)
    parser.add_argument("--game-time-limit", type=positive_float, default=1200)
    parser.add_argument("--realtime", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--parallel", type=int, default=4, help="Maximum simultaneous game subprocesses")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.games < 1:
        parser.error("--games must be positive")
    if args.parallel < 1:
        parser.error("--parallel must be positive")
    os.environ.setdefault("SC2PATH", r"C:\game\StarCraft II")
    os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "localhost", "127.0.0.1"]))
    from sc2 import maps
    directory = args.output_dir.resolve()
    fingerprints = {str((SOURCE_DIR / name).relative_to(REPO)): sha256(SOURCE_DIR / name) for name in SOURCES}
    protocol = {"policy": "uniform_random_legal", "planner": "none", "map": args.map,
                "map_sha256": hashlib.sha256(maps.get(args.map).data).hexdigest(),
                "difficulty": args.difficulty, "opponent_race": "Zerg", "realtime": args.realtime,
                "decision_timebase": "wall" if args.realtime else "game", "parallel": args.parallel,
                "seeds": list(range(args.start_seed, args.start_seed + args.games)),
                "policy_seed_rule": "same_as_game_seed_independent_rng", "game_time_limit": args.game_time_limit,
                "decision_interval": 1.0, "max_requests": 2000, "includes_wait": True,
                "source_fingerprints": fingerprints}
    trials = []
    if args.resume:
        if read_json(directory / "protocol.json") != protocol:
            parser.error("Resume configuration or runtime sources differ from frozen protocol")
        trials = read_json(directory / "results.json")["trials"]
    else:
        directory.mkdir(parents=True, exist_ok=False)
        atomic_json(directory / "protocol.json", protocol)
        for relative in fingerprints:
            target = directory / "runtime-source-snapshot" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REPO / relative).read_bytes())
    write_results(directory, protocol, trials, "running")
    pending_seeds = [seed for seed in protocol["seeds"]
                     if not any(t["seed"] == seed and t.get("valid") for t in trials)]

    def run_trial(seed):
        attempt = 1 + sum(t["seed"] == seed for t in trials)
        run = directory / f"seed-{seed:03d}-attempt-{attempt:02d}"
        command = [sys.executable, "-X", "utf8", "-B", "-m", "sc2_rl_agent.starcraftenv_test.run_jev",
                   "--policy", "random", "--planner", "none", "--seed", str(seed), "--policy-seed", str(seed),
                   "--map", protocol["map"], "--opponent-race", "Zerg", "--difficulty", args.difficulty,
                   "--decision-interval", "1", "--max-requests", str(protocol["max_requests"]),
                   "--realtime" if args.realtime else "--no-realtime",
                   "--game-time-limit", str(args.game_time_limit), "--output-dir", str(run)]
        if any(sha256(REPO / name) != digest for name, digest in fingerprints.items()):
            raise RuntimeError("Frozen runtime sources changed during the experiment")
        print(f"Starting seed {seed}: {run}", flush=True)
        with (directory / f"{run.name}.console.log").open("x", encoding="utf-8") as console:
            completed = subprocess.run(command, cwd=REPO, stdout=console, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return run, completed.returncode

    # Each worker owns a separate Python process and SC2 client. Only the parent
    # decodes replays and writes aggregate results, avoiding shared SDK/game state.
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {executor.submit(run_trial, seed): seed for seed in pending_seeds}
        for future in as_completed(futures):
            seed = futures[future]
            run, exit_code = future.result()
            try:
                trial = audit_trial(run, protocol, seed, seed)
            except Exception as exc:
                trial = {"run": run.name, "seed": seed, "policy_seed": seed, "valid": False,
                         "error": f"{type(exc).__name__}: {exc}"}
            trial["process_exit_code"] = exit_code
            trials.append(trial)
            trials.sort(key=lambda t: (t["seed"], t["run"]))
            write_results(directory, protocol, trials, "running")
            print(json.dumps(trial, ensure_ascii=False), flush=True)
    aggregate = summarize(trials, args.games)
    status = "completed" if aggregate["verified_games"] == args.games else "incomplete"
    write_results(directory, protocol, trials, status)
    print(json.dumps(aggregate, ensure_ascii=False), flush=True)
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
