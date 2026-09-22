"""Run the Jev Protoss macro agent against the built-in SC2 AI in realtime."""

import argparse
import asyncio
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

from .agent.jev_agent import JevClient, load_api_key
from .agent.macro_contract import VERSION
from .utils.run_logging import RunLog, atomic_json


def positive_float(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default="Altitude LE")
    parser.add_argument("--opponent-race", choices=["Zerg", "Terran", "Protoss", "Random"], default="Zerg")
    parser.add_argument("--difficulty", choices=["VeryEasy", "Easy", "Medium", "MediumHard", "Hard", "Harder", "VeryHard", "CheatVision", "CheatMoney", "CheatInsane"], default="Easy")
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--decision-interval", type=positive_float, default=1.0, help="Minimum wall seconds between request starts")
    parser.add_argument("--request-timeout", type=positive_float, default=2.5)
    parser.add_argument("--max-decision-age", type=positive_float, default=4.0, help="Maximum age in both wall and game seconds")
    parser.add_argument("--max-requests", type=int, default=2000)
    parser.add_argument("--game-time-limit", type=positive_float, default=1200, help="Stop after this many game seconds; a time limit is a Tie")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--sc2-path", type=Path)
    parser.add_argument("--config-file", type=Path, default=repo.parent / "config.md", help="Fallback for api: entry; TYPESAFE_API_KEY takes precedence")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--planner", choices=["none", "codex"], default="none")
    parser.add_argument("--planner-model", default="gpt-6-astra")
    parser.add_argument("--codex-path", type=Path, help="Optional native Codex executable; uses saved Codex login")
    parser.add_argument("--planner-effort", choices=["low", "medium", "high", "xhigh", "max"], default="low")
    parser.add_argument("--planner-interval", type=positive_float, default=60, help="Game seconds between periodic strategic requests")
    parser.add_argument("--planner-execution-window", type=positive_float, default=30, help="Game seconds to execute an accepted plan before ordinary events refresh it")
    parser.add_argument("--planner-min-interval", type=positive_float, default=10, help="Minimum wall seconds between planner requests, including events")
    parser.add_argument("--planner-event-cooldown", type=positive_float, default=30, help="Wall seconds before the same event type can request another update")
    parser.add_argument("--planner-timeout", type=positive_float, default=60, help="Wall seconds before killing a stalled Codex request")
    parser.add_argument("--plan-ttl", type=positive_float, default=180, help="Game seconds a received plan stays valid")
    parser.add_argument("--max-plan-age", type=positive_float, default=60)
    parser.add_argument("--max-planner-requests", type=int, default=80)
    args = parser.parse_args()
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    if args.max_planner_requests < 1:
        parser.error("--max-planner-requests must be positive")
    try:
        key = load_api_key(args.config_file)
    except ValueError as exc:
        parser.error(str(exc))
    if args.sc2_path:
        os.environ["SC2PATH"] = str(args.sc2_path.resolve())
    elif not os.environ.get("SC2PATH") and Path(r"C:\game\StarCraft II").is_dir():
        os.environ["SC2PATH"] = r"C:\game\StarCraft II"

    output = args.output_dir or repo / "jev_runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # Never silently overwrite another run.
    settings = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
                if k != "config_file"}
    settings.update(realtime=True, player_race="Protoss", output_dir=str(output), macro_contract=VERSION)
    source_dir = Path(__file__).resolve().parent
    sources = [source_dir / name for name in (
        "agent/astra_planner.py", "agent/jev_agent.py", "agent/strategic_policy.py", "agent/macro_contract.py",
        "env/bot/Protoss_bot.py", "env/bot/jev_protoss_bot.py", "env/bot/hierarchical_protoss_bot.py",
        "env/bot/macro_execution.py", "env/bot/macro_navigation.py", "run_jev.py", "utils/run_logging.py",
        "utils/sc2_runtime.py", "utils/action_info.py")]
    atomic_json(output / "source-fingerprints.json", {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    log = RunLog(output, settings, secrets=(key,), console=sys.stdout)
    client = planner_client = bot = None
    result_name, status = "interrupted", "interrupted"
    try:
        # Captures legacy prints and SDK stderr, including startup/import failures.
        with log.capture_engine():
            try:
                # --help, report rebuilding and argument checks need no SC2 installation.
                from sc2 import maps
                from sc2.data import Difficulty, Race
                from .utils.sc2_runtime import run_windowed_game
                from sc2.player import Bot, Computer
                from .env.bot.jev_protoss_bot import JevProtossBot

                game_map = maps.get(args.map)
                log("environment", map_path=str(game_map.path), map_sha256=hashlib.sha256(game_map.data).hexdigest(),
                    macro_contract=VERSION)
                client = JevClient(key, args.model, args.request_timeout)
                if args.planner == "codex":
                    from .agent.astra_planner import CodexPlannerClient
                    from .env.bot.hierarchical_protoss_bot import HierarchicalProtossBot
                    planner_client = CodexPlannerClient(output, args.codex_path, args.planner_model,
                                                       args.planner_timeout, args.planner_effort)
                    bot = HierarchicalProtossBot(client, output, args.decision_interval, args.max_decision_age,
                                                 args.max_requests, planner_client=planner_client, run_log=log,
                                                 planner_interval=args.planner_interval, plan_ttl=args.plan_ttl,
                                                 planner_min_interval=args.planner_min_interval,
                                                 planner_event_cooldown=args.planner_event_cooldown,
                                                 planner_execution_window=args.planner_execution_window,
                                                 max_plan_age=args.max_plan_age, max_planner_requests=args.max_planner_requests)
                else:
                    bot = JevProtossBot(client, output, args.decision_interval, args.max_decision_age,
                                       args.max_requests, run_log=log)
                log.phase = "launching"
                result = run_windowed_game(game_map, [Bot(Race.Protoss, bot),
                                           Computer(Race[args.opponent_race], Difficulty[args.difficulty])],
                                           realtime=True, game_time_limit=args.game_time_limit,
                                           random_seed=args.seed, save_replay_as=str(output / "game.SC2Replay"),
                                           event_sink=log)
                result_name, status = result.name, "completed"
            except (Exception, KeyboardInterrupt) as exc:
                log.fail(exc)
            finally:
                # The legacy bot imports nest_asyncio; run_game and cleanup share its loop.
                try:
                    if bot is not None:
                        asyncio.run(bot.shutdown(result_name))
                    else:
                        if planner_client is not None:
                            asyncio.run(planner_client.close())
                        if client is not None:
                            asyncio.run(client.close())
                except Exception as exc:
                    log.fail(exc)
        log.finish(status, result_name)
        from .run_report import build_report
        try:
            build_report(output)
        except Exception as exc:
            # Original events/summary remain usable and the report can be rebuilt.
            log("report_error", error=type(exc).__name__)
            log.write_summary(log.summary)
            print(f"Report generation failed; rebuild with run_report. Raw logs: {output}", flush=True)
        else:
            print(f"Report: {output / 'report.html'}", flush=True)
        return 0 if log.status == "completed" else 130 if log.status == "interrupted" else 1
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
