"""Export the selected realtime macro batches without changing their raw archives.

Only standard-library dependencies are needed. Each recorded event/replay hash is
checked before a derived result is exported. This script never starts games or
contacts a model.
"""

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODES = [
    ("random", "Pure random", "none", "random", "none"),
    ("jev", "JEV-only", "none", "jev", "none"),
    ("astra_constrained_random", "Astra constrained + random", "astra", "random", "constrained"),
    ("astra_constrained_jev", "Astra constrained + JEV", "astra", "jev", "constrained"),
    ("astra_advisory_jev", "Astra advisory + JEV", "astra", "jev", "advisory"),
]
BATCHES = {
    "random": "random-macro-realtime-lv7-20260924-10-seeds",
    "jev": "jev-only-realtime-lv7-20260924-10-seeds-r2",
    "astra_constrained_random": "astra-random-lv7-20260923-10-seeds",
    "astra_constrained_jev": "astra-constrained-jev-lv7-20260924-10-seeds-r3",
    "astra_advisory_jev": "astra-advisory-guard20-jev-lv7-20260924-10-seeds",
}
PROTOCOL_KEYS = (
    "map", "map_sha256", "difficulty", "opponent_race", "realtime", "seeds",
    "decision_timebase", "decision_interval", "game_time_limit", "max_requests",
    "parallel", "policy_seed_rule", "planner", "policy", "model", "planner_model",
    "planner_effort", "plan_mode", "planner_interval", "plan_ttl", "request_timeout",
    "max_decision_age", "planner_execution_window", "planner_min_interval",
    "planner_event_cooldown", "planner_timeout", "max_plan_age", "max_planner_requests",
    "advisory_posture_hold", "source_commit", "sc2_id_version",
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 6)


def wilson(wins, games):
    if not games:
        return None
    z = 1.959963984540054
    p = wins / games
    denominator = 1 + z * z / games
    center = (p + z * z / (2 * games)) / denominator
    radius = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def event_metrics(path):
    counts, advanced = Counter(), Counter()
    latencies = {"decision": [], "planner": []}
    requests = {"decision": {}, "planner": {}}
    candidates, heartbeats, intervals = [], [], []
    previous_request = None
    for line in path.open(encoding="utf-8"):
        event = json.loads(line)
        kind = event["event"]
        counts[kind] += 1
        if kind in {"request", "planner_request"}:
            role = "decision" if kind == "request" else "planner"
            requests[role][event["request_id"]] = event["game_loop"]
        if kind == "request":
            candidates.append(len(event["payload"]["questions"]["next_macro_action"]["criteria"]))
            if previous_request is not None:
                intervals.append(event["wall_seconds"] - previous_request)
            previous_request = event["wall_seconds"]
        if kind in {"response", "planner_response"}:
            role = "decision" if kind == "response" else "planner"
            latencies[role].append(event["latency_ms"] / 1000)
            start = requests[role][event["request_id"]]
            advanced[role] += event["game_loop"] > start
        if kind == "heartbeat":
            heartbeats.append((event["wall_seconds"], event["game_seconds"]))
    elapsed_wall = heartbeats[-1][0] - heartbeats[0][0]
    elapsed_game = heartbeats[-1][1] - heartbeats[0][1]
    return {
        "requests": counts["request"], "responses": counts["response"],
        "api_errors": counts["api_error"], "request_cancellations": counts["request_cancelled"],
        "planner_requests": counts["planner_request"], "planner_responses": counts["planner_response"],
        "accepted_plans": counts["plan_accepted"],
        "decision_responses_with_game_advance": advanced["decision"],
        "planner_responses_with_game_advance": advanced["planner"],
        "candidate_count_mean": statistics.mean(candidates),
        "minimum_request_interval_wall_s": min(intervals),
        "heartbeat_game_to_wall_ratio": round(elapsed_game / elapsed_wall, 6),
    }, latencies, candidates


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def jev_only_behavior(directory, trials):
    """Derive standalone behavior from requests and replies, never from pilot games."""
    rows = []
    for trial in sorted(trials, key=lambda item: item["seed"]):
        requests, choices, offered, errors = {}, Counter(), Counter(), Counter()
        workers, army, minerals = [], [], []
        peak_structures = Counter()
        wait_with_resources = advancing = rapid_defend = cancellations = 0
        previous_army_order = None
        latencies = []
        for line in (directory / trial["run"] / "events.jsonl").open(encoding="utf-8"):
            event = json.loads(line)
            kind = event["event"]
            if kind == "request":
                state = event["payload"]["state"]
                assert event.get("plan_id") is None
                assert not any(state.get(key) for key in ("strategic_plan", "strategy_status", "execution_directive"))
                requests[event["request_id"]] = event
                offered.update(event["payload"]["questions"]["next_macro_action"]["criteria"].keys())
            elif kind == "response":
                assert event["model"] == "jev-1.13.0" and event.get("plan_id") is None
                request = requests[event["request_id"]]
                candidates = request["payload"]["questions"]["next_macro_action"]["criteria"]
                choice = event["answer"]["choice"]
                assert choice in candidates
                choices[choice] += 1
                advancing += event["game_loop"] > request["game_loop"]
                latencies.append(event["latency_ms"] / 1000)
                if choice == "71":
                    wait_with_resources += (request["payload"]["state"]["resource"]["mineral"] >= 500
                                            and any(0 <= int(action) <= 59 for action in candidates))
            elif kind == "api_error":
                errors[event["error"]] += 1
            elif kind == "request_cancelled":
                cancellations += 1
            elif kind == "action_mask":
                assert not any(reason.startswith("plan_") for reason in event["blocked"].values())
            elif kind == "heartbeat":
                workers.append(event["economy"]["worker_supply"])
                army.append(event["economy"]["ready_army_supply"])
                minerals.append(event["economy"]["mineral"])
                for name, count in event["structures"].items():
                    peak_structures[name] = max(peak_structures[name], count)
            elif kind == "action" and event.get("intent_applied"):
                if previous_army_order:
                    rapid_defend += (previous_army_order["action_id"] == 64 and event["action_id"] == 72
                                     and event["game_seconds"] - previous_army_order["game_seconds"] <= 5)
                previous_army_order = event
            assert not kind.startswith(("planner_", "plan_")), "Unexpected Astra event in standalone batch"
        responses = sum(choices.values())
        assert len(requests) == responses + sum(errors.values()) + cancellations
        assert advancing == responses
        rows.append({
            "seed": trial["seed"], "result": trial["result"], "game_seconds": round(trial["game_seconds"], 6),
            "requests": len(requests), "responses": responses, "api_errors": dict(errors),
            "request_cancellations": cancellations, "response_frames_advanced": advancing,
            "wait_choices": choices["71"], "wait_fraction": round(choices["71"] / responses, 6),
            "waits_with_500_minerals_and_production_available": wait_with_resources,
            "candidate_offers": dict(offered), "chosen_actions": dict(choices),
            "peak_workers": max(workers), "peak_ready_army_supply": max(army), "peak_minerals": max(minerals),
            "peak_observed_structures": dict(peak_structures), "rapid_defend_after_attack_within_5s": rapid_defend,
            "response_latency_s_p50": percentile(latencies, .5), "response_latency_s_p95": percentile(latencies, .95),
            "events_sha256": trial["events_sha256"], "replay_sha256": trial["replay_sha256"],
        })
    return {
        "batch": directory.name, "trials": rows,
        "definitions": {
            "wait_fraction": "EMPTY ACTION replies divided by all valid JEV replies.",
            "resource_wait": "WAIT with at least 500 minerals and an offered train/build/research action (IDs 0-59); not proof that every wait is erroneous.",
            "structure_peaks": "Maximum simultaneously observed counts at five-game-second heartbeats; submitted orders do not prove completion.",
            "rapid_defend": "Consecutive applied army commands: attack 64 then ordinary defense 72 within five game seconds.",
        },
    }


def jev_pilot_evidence(runs_root):
    """Publish hashes and technical outcomes without copying private archive paths."""
    directory = runs_root / "jev-only-realtime-lv7-20260924-10-seeds"
    disclosure = read(directory / "technical-abort-001.json")
    assert digest(directory / "results.json") == disclosure["original_results_sha256"]
    assert digest(directory / "protocol.json") == disclosure["original_protocol_sha256"]
    rows = []
    for run in sorted(path for path in directory.glob("seed-*-attempt-*") if path.is_dir()):
        summary = read(run / "summary.json")
        rows.append({"run": run.name, "status": summary["status"], "result": summary["result"],
                     "failure": summary.get("failure"),
                     "events_sha256": digest(run / "events.jsonl"),
                     "replay_sha256": digest(run / "game.SC2Replay") if (run / "game.SC2Replay").exists() else None})
    assert len(rows) == 4
    return {"batch": directory.name, "classification": disclosure["classification"],
            "reason": disclosure["reason"], "trials": rows,
            "disclosure_sha256": digest(directory / "technical-abort-001.json"),
            "protocol_sha256": disclosure["original_protocol_sha256"],
            "results_sha256": disclosure["original_results_sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "paper/data/macro_realtime")
    args = parser.parse_args()
    games, summaries, manifests = [], [], []
    for mode, label, planner, selector, plan_mode in MODES:
        directory = args.runs_root / BATCHES[mode]
        results = read(directory / "results.json")
        protocol = read(directory / "protocol.json")
        assert protocol["realtime"] is True
        assert protocol["map"] == "Altitude LE" and protocol["difficulty"] == "VeryHard"
        assert protocol["opponent_race"] == "Zerg" and protocol["game_time_limit"] == 1200
        assert protocol["seeds"] == list(range(1, 11))
        assert len(results["trials"]) == 10
        outcomes, evidence = Counter(), Counter()
        pooled_latencies = {"decision": [], "planner": []}
        pooled_candidates, batch_games = [], []
        for trial in sorted(results["trials"], key=lambda item: item["seed"]):
            run = directory / trial["run"]
            seed = trial["seed"]
            assert len([path for path in directory.glob(f"seed-{seed:03d}-attempt-*") if path.is_dir()]) == 1
            events_hash = digest(run / "events.jsonl")
            assert events_hash == trial["events_sha256"], (mode, seed, "event hash")
            verified = trial["valid"]
            result = trial.get("reported_result", trial["result"])
            if verified:
                assert all(trial["checks"].values())
                assert digest(run / "game.SC2Replay") == trial["replay_sha256"], (mode, seed, "replay hash")
                source = "replay"
            else:
                assert mode == "astra_constrained_random" and seed == 5
                assert trial["reported_result_source"] == "user_confirmation"
                assert result == "Defeat" and trial.get("replay_sha256") is None
                assert not (run / "game.SC2Replay").exists()
                source = "human_adjudication"
            metrics, latencies, candidates = event_metrics(run / "events.jsonl")
            assert metrics["minimum_request_interval_wall_s"] >= .99
            if planner == "none":
                assert metrics["planner_requests"] == 0
            for role in pooled_latencies:
                pooled_latencies[role].extend(latencies[role])
            pooled_candidates.extend(candidates)
            row = {
                "configuration": mode, "batch": BATCHES[mode], "seed": seed,
                "run": trial["run"], "result": result, "raw_audit_result": trial["result"],
                "result_evidence": source, "replay_verified": verified,
                "game_seconds": round(trial["game_seconds"], 6), **metrics,
                "events_sha256": events_hash, "replay_sha256": trial.get("replay_sha256"),
            }
            games.append(row)
            batch_games.append(row)
            outcomes[result] += 1
            evidence[source] += 1
        assert [row["seed"] for row in batch_games] == list(range(1, 11))
        expected = results.get("reported_aggregate", results["aggregate"])
        assert (outcomes["Victory"], outcomes["Defeat"], outcomes["Tie"]) == (
            expected["wins"], expected["losses"], expected["ties"])
        summary = {
            "configuration": mode, "label": label, "planner": planner, "selector": selector,
            "plan_mode": plan_mode, "evaluated_at_lv7": True, "games": 10,
            "wins": outcomes["Victory"], "losses": outcomes["Defeat"], "time_limits": outcomes["Tie"],
            "win_rate": outcomes["Victory"] / 10, "replay_verified_games": evidence["replay"],
            "human_adjudicated_games": evidence["human_adjudication"],
            "note": "Nine replay-verified losses and one human-adjudicated loss." if evidence["human_adjudication"] else "",
        }
        summaries.append(summary)
        metrics = {key: sum(row[key] for row in batch_games) for key in (
            "requests", "responses", "api_errors", "request_cancellations", "planner_requests", "planner_responses", "accepted_plans",
            "decision_responses_with_game_advance", "planner_responses_with_game_advance")}
        metrics.update(
            decision_latency_s_p50=percentile(pooled_latencies["decision"], .5),
            decision_latency_s_p95=percentile(pooled_latencies["decision"], .95),
            planner_latency_s_p50=percentile(pooled_latencies["planner"], .5),
            planner_latency_s_p95=percentile(pooled_latencies["planner"], .95),
            candidate_count_mean=statistics.mean(pooled_candidates),
            win_rate_95pct_wilson=wilson(outcomes["Victory"], 10),
            minimum_request_interval_wall_s=min(row["minimum_request_interval_wall_s"] for row in batch_games),
        )
        manifests.append({
            "configuration": mode, "batch": BATCHES[mode], "results_sha256": digest(directory / "results.json"),
            "protocol_sha256": digest(directory / "protocol.json"),
            "protocol": {key: protocol[key] for key in PROTOCOL_KEYS if key in protocol},
            "source_fingerprints": protocol["source_fingerprints"],
            "sdk_fingerprints": protocol.get("sc2_sdk_fingerprints", {}),
            "runtime_revisions": [{key: item[key] for key in ("name", "seeds", "source_fingerprints") if key in item}
                                  for item in protocol.get("runtime_revisions", [])],
            "transport_evidence": {key: protocol.get("transport_bootstrap", {})[key]
                                   for key in ("sha256", "provider_settings_sha256", "credentials_copied")
                                   if key in protocol.get("transport_bootstrap", {})},
            "metrics": metrics,
        })
        print(json.dumps({"configuration": mode, "outcomes": dict(outcomes), "metrics": metrics}), flush=True)
    assert len(games) == 50 and sum(row["replay_verified"] for row in games) == 49
    assert len({row["replay_sha256"] for row in games if row["replay_verified"]}) == 49
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "games.csv", games)
    write_csv(output / "configurations.csv", summaries)
    manifest = {
        "edition": "2026-09-24", "scope": "Five configurations; five recorded ten-seed realtime Lv7 batches.",
        "games": 50, "replay_verified_games": 49, "human_adjudicated_games": 1,
        "source_events_and_replay_hashes_rechecked": True, "configurations": summaries, "batches": manifests,
        "interpretation": "Fixed ten-seed development samples; versions and service timing differ across batches. No isolated causal effect is claimed.",
        "excluded": [
            "Historical accelerated random batch", "Earlier advisory batch",
            "Constrained transport pilots r1/r2", "Historical Lv2 JEV-only game",
            "Entire four-attempt JEV-only pilot with unsynchronized SC2 SDK IDs; unscored service preflight calls",
        ],
        "excluded_jev_pilot": jev_pilot_evidence(args.runs_root),
    }
    jev_directory = args.runs_root / BATCHES["jev"]
    jev_results = read(jev_directory / "results.json")
    assert jev_results["status"] == "completed" and all(trial["valid"] for trial in jev_results["trials"])
    preflights = []
    for path in sorted(jev_directory.glob("preflight-unscored-attempt-*.json")):
        attempt = read(path)
        preflights.append({"file": path.name, "success": attempt["success"],
                          "error": attempt.get("error"), "sha256": digest(path)})
    assert len(preflights) == 5 and sum(item["success"] for item in preflights) == 1
    assert all(item["success"] or item["error"] == "request_timeout" for item in preflights)
    manifest["unscored_jev_preflights"] = preflights
    behavior = jev_only_behavior(jev_directory, jev_results["trials"])
    (output / "jev_only_behavior.json").write_text(json.dumps(behavior, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(games)} games and {len(summaries)} configurations to {output}")


if __name__ == "__main__":
    main()
