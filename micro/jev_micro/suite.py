"""Run reproducible, resumable SMAC-Hard map sweeps."""

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .catalog import SUPPORTED_MAPS, DEVELOPMENT_MAPS, inventory, map_param_registry
from .run import REPO, positive, write_json
from .planner import add_planner_arguments


def result_row(map_name, policy, output, returncode):
    summary_file = output / "summary.json"
    summary = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.is_file() else {}
    episodes = [e for e in summary.get("episodes", []) if e["status"] in {"win", "loss", "draw", "timeout"}]
    def mean(fn):
        return round(statistics.mean(fn(e) for e in episodes), 2) if episodes else None
    return {
        "map": map_name, "policy": policy, "returncode": returncode,
        "failure": summary.get("failure") or ("process_failed" if returncode else None),
        "summary_file": str(summary_file), "completed_episodes": len(episodes),
        "wins": sum(e["status"] == "win" for e in episodes),
        "statuses": [e["status"] for e in summary.get("episodes", [])],
        "mean_enemy_kills": mean(lambda e: e["initial"]["enemies_alive"] - e["final"]["enemies_alive"]),
        "mean_allies_alive": mean(lambda e: e["final"]["allies_alive"]),
        "mean_enemy_health_and_shield": mean(lambda e: e["final"]["enemy_health"] + e["final"]["enemy_shield"]),
        "mean_allied_health_and_shield": mean(lambda e: e["final"]["allied_health"] + e["final"]["allied_shield"]),
        "mean_steps": mean(lambda e: e["steps"]),
        "requests": summary.get("requests", 0),
        "latency_median_ms": summary.get("latency_median_ms"),
        "api_errors": summary.get("api_errors", 0),
        "sc2_action_errors": summary.get("sc2_action_errors", 0),
        "fixed_step_checks": summary.get("fixed_step_checks", 0),
        "input_tokens": summary.get("input_tokens", 0),
        "output_tokens": summary.get("output_tokens", 0),
        "estimated_api_cost_usd": summary.get("estimated_api_cost_usd", 0),
        "development_map": map_name in DEVELOPMENT_MAPS,
        "planner_result": summary.get("planner_result"),
        "planner_fixed_step_checks": summary.get("planner_fixed_step_checks", 0),
    }


def save_report(output, settings, results, startup_failed=False):
    invalid_file = output / "invalidated-runs.json"
    invalidated = json.loads(invalid_file.read_text(encoding="utf-8")) if invalid_file.exists() else {}
    excluded_cost = sum(json.loads(Path(row["summary_file"]).read_text(encoding="utf-8")).get("estimated_api_cost_usd", 0)
                        for row in invalidated.get("invalidated_runs", []))
    summary = {**settings, "results": results, "stopped_after_startup_error": startup_failed,
               "planner_requests": sum((r.get("planner_result") or {}).get("requests", 0) for r in results),
               "planner_successful_plans": sum((r.get("planner_result") or {}).get("successful_responses", 0) for r in results),
               "completed_episodes": sum(r["completed_episodes"] for r in results),
               "wins": sum(r["wins"] for r in results),
               "estimated_api_cost_usd": round(sum(r["estimated_api_cost_usd"] for r in results), 9),
               "excluded_attempt_cost_usd": round(excluded_cost, 9),
               "total_cost_including_excluded_usd": round(sum(r["estimated_api_cost_usd"] for r in results) + excluded_cost, 9)}
    write_json(output / "summary.json", summary)
    lines = ["# SMAC-Hard map smoke tests", "",
             "Base opponent, shared team vision, fixed-step simulation, abilities disabled. Three episodes per map are exploratory results, not reliable win-rate estimates.", "",
             "Maps marked dev are developer scenarios. Archived maps and dev scenarios use explicit attack-move extensions where the upstream base script has no implementation.", "",
             "| Map | Policy | Wins/completed | Results | Mean kills | Mean survivors | JEV USD | Astra plans |",
             "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    for row in results:
        link = Path(row["summary_file"]).relative_to(output).as_posix()
        label = row["map"] + (" (dev)" if row["development_map"] else "")
        lines.append(f"| {label} | [{row['policy']}]({link}) | {row['wins']}/{row['completed_episodes']} "
                     f"| {', '.join(row['statuses'])} | {row['mean_enemy_kills']} | {row['mean_allies_alive']} "
                     f"| {row['estimated_api_cost_usd']:.5f} | {(row.get('planner_result') or {}).get('successful_responses', 0)} |")
    if settings.get("planner") == "codex":
        lines.append("\nOne Astra map plan is reused in every JEV request across all episodes. "
                     "JEV cost estimates exclude Codex account usage; Astra token counts are in each planner_result.")
    for row in results:
        if row["failure"]:
            lines.append(f"\nRun error ({row['map']}/{row['policy']}): {row['failure']}")
    if startup_failed:
        lines.append("\nRemaining runs were not attempted because SC2 could not start. Resume after resolving the startup error.")
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps", nargs="+", choices=SUPPORTED_MAPS, default=["8m", "3s_vs_3z", "2s3z"])
    parser.add_argument("--all-maps", action="store_true")
    parser.add_argument("--policies", nargs="+", choices=["jev", "nearest"], default=["nearest", "jev"])
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--step-mul", type=int, default=8)
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--request-timeout", type=positive, default=10.0)
    parser.add_argument("--max-requests", type=int, help="Per-map limit; otherwise derived from map step limit and maximum unit batches")
    parser.add_argument("--batch-concurrency", type=int, default=4)
    parser.add_argument("--api-retries", type=int, default=2)
    parser.add_argument("--sc2-path", type=Path, default=Path(os.environ.get("SC2PATH", r"C:\game\StarCraft II")))
    parser.add_argument("--config-file", type=Path, default=REPO.parent / "config.md")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true", help="Keep completed cases, preserve failed attempts and retry them")
    add_planner_arguments(parser)
    args = parser.parse_args()
    if args.all_maps:
        args.maps = list(SUPPORTED_MAPS)
    if min(args.episodes, args.step_mul, args.batch_concurrency) < 1 or args.api_retries < 0 or (args.max_requests is not None and args.max_requests < 1):
        parser.error("episodes, step-mul, concurrency and max-requests must be positive; retries cannot be negative")
    if len(set(args.maps)) != len(args.maps):
        parser.error("maps must be unique")
    if len(set(args.policies)) != len(args.policies):
        parser.error("policies must be unique")
    if args.resume and args.output_dir is None:
        parser.error("--resume requires --output-dir")
    output = (args.output_dir or REPO / "jev_runs" / datetime.now().strftime("maps-%Y%m%d-%H%M%S-%f")).resolve()
    output.mkdir(parents=True, exist_ok=args.resume)
    settings = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items() if k != "config_file"}
    settings.update(output_dir=str(output), opponent="base", realtime=False, use_ability=False,
                    planned_runs=len(args.policies) * len(args.maps),
                    planned_episodes=len(args.policies) * len(args.maps) * args.episodes)
    suite_file = output / "suite.json"
    if args.resume and suite_file.exists():
        previous = json.loads(suite_file.read_text(encoding="utf-8"))
        for key in ("maps", "policies", "episodes", "seed", "step_mul", "model", "opponent"):
            if previous.get(key) != settings.get(key):
                parser.error(f"Resume settings differ: {key}")
        if previous.get("planner", "none") != settings["planner"]:
            parser.error("Resume settings differ: planner")
        if settings["planner"] == "codex":
            for key in ("planner_model", "planner_effort"):
                if previous.get(key) != settings[key]:
                    parser.error(f"Resume settings differ: {key}")
    else:
        write_json(suite_file, settings)
        write_json(output / "inventory.json", inventory())
    results = []
    invalid_file = output / "invalidated-runs.json"
    invalidated = json.loads(invalid_file.read_text(encoding="utf-8")) if invalid_file.exists() else {}
    invalid_paths = {row["summary_file"] for row in invalidated.get("invalidated_runs", [])}
    startup_failed = False
    print(f"Suite output: {output}", flush=True)
    for map_name in args.maps:
        for policy in args.policies:
            case_output = output / map_name / policy
            if args.resume:
                completed_attempts = [case_output, *sorted(case_output.parent.glob(policy + "-retry-*"))]
                reusable = None
                for attempt_output in completed_attempts:
                    if (attempt_output / "summary.json").exists() and str(attempt_output / "summary.json") not in invalid_paths:
                        row = result_row(map_name, policy, attempt_output, 0)
                        if not row["failure"] and row["completed_episodes"] == args.episodes:
                            reusable = row
                if reusable:
                    results.append(reusable)
                    save_report(output, settings, results)
                    print(f"Keeping completed {map_name} / {policy}", flush=True)
                    continue
                if case_output.exists():
                    case_output = case_output.parent / (policy + "-retry-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            params = map_param_registry[map_name]
            max_requests = args.max_requests or args.episodes * params["limit"] * params["n_agents"] * (args.api_retries + 1)
            command = [sys.executable, "-X", "utf8", "-B", "-m", "jev_micro.run",
                       "--map", map_name, "--policy", policy, "--opponent", "base",
                       "--episodes", str(args.episodes), "--seed", str(args.seed),
                       "--step-mul", str(args.step_mul), "--model", args.model,
                       "--request-timeout", str(args.request_timeout),
                       "--max-requests", str(max_requests), "--sc2-path", str(args.sc2_path),
                       "--batch-concurrency", str(args.batch_concurrency), "--api-retries", str(args.api_retries),
                       "--config-file", str(args.config_file.resolve()), "--output-dir", str(case_output)]
            if args.planner == "codex" and policy == "jev":
                command.extend(["--planner", "codex", "--planner-model", args.planner_model,
                                "--planner-effort", args.planner_effort,
                                "--planner-timeout", str(args.planner_timeout)])
                if args.codex_path:
                    command.extend(["--codex-path", str(args.codex_path.resolve())])
            print(f"Starting {map_name} / {policy}", flush=True)
            with (output / f"{map_name}-{case_output.name}.log").open("w", encoding="utf-8") as log:
                with subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace") as process:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        if line.startswith(("SMAC-Hard ", "Output:", "Client branch:", "Episode ", "Summary:", "Run stopped:", "Planning ", "Plan accepted ")):
                            print(line, end="", flush=True)
                    returncode = process.wait()
            row = result_row(map_name, policy, case_output, returncode)
            results.append(row)
            startup_failed = (row["failure"] in {"ConnectError", "SC2LaunchError"}
                              and row["completed_episodes"] == 0 and row["requests"] == 0)
            save_report(output, settings, results, startup_failed)
            print(f"Finished {map_name} / {policy}: {row['wins']}/{row['completed_episodes']} wins, failure={row['failure']}", flush=True)
            if startup_failed:
                print("SC2 could not start; stopping the suite before launching other maps.", flush=True)
                break
        if startup_failed:
            break
    save_report(output, settings, results, startup_failed)
    print(f"Comparison: {output / 'comparison.md'}", flush=True)
    if any(row["returncode"] for row in results):
        parser.exit(1, "Some runs failed; see each run's log and summary.\n")


if __name__ == "__main__":
    from .windows_session import active_display
    with active_display():
        main()
