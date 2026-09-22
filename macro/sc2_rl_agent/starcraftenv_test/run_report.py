"""Build an offline SC2 replay report from v1 or v2 logs; no game/API required."""

import argparse
import csv
import html
import json
import os
from collections import Counter
from pathlib import Path

from .utils.run_logging import PRICING, RunMetrics, atomic_json, estimate_cost, request_key


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}


def analyze_run(directory):
    directory = Path(directory)
    settings = read_json(directory / "run.json")
    summary = read_json(directory / "summary.json")
    metrics = RunMetrics(settings.get("pricing", PRICING))
    rows, heartbeats, plans, issues = {}, [], [], []
    events = Counter()
    last_seq, run_id = 0, settings.get("run_id")
    legacy = False
    with (directory / "events.jsonl").open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                    raise ValueError("not an event object")
            except (ValueError, TypeError):
                issues.append({"line": line_number, "code": "unreadable_event", "detail": "Skipped invalid/truncated JSON line"})
                continue
            kind = event["event"]
            events[kind] += 1
            legacy |= "schema_version" not in event
            if "seq" in event:
                if event["seq"] != last_seq + 1:
                    issues.append({"line": line_number, "code": "sequence_gap", "detail": str(event["seq"])})
                last_seq = event["seq"]
            if run_id and event.get("run_id", run_id) != run_id:
                issues.append({"line": line_number, "code": "mixed_run_ids", "detail": event["run_id"]})
            metrics.observe(event)
            if kind == "end":
                summary = {**event, **summary}
            if kind == "run_failure":
                summary.setdefault("failure", event.get("failure"))
            if kind == "run_end":
                summary.update(status=event.get("status"), result=event.get("result"))
            if kind == "heartbeat":
                heartbeats.append({"game_seconds": event.get("game_loop", 0) / 22.4,
                                   "economy": event.get("economy", {}), "units": event.get("units", {}),
                                   "structures": event.get("structures", {})})
            if kind in {"plan_accepted", "plan_progress", "plan_expired", "army_intent_changed", "planner_trigger", "planner_discarded"}:
                plans.append(event)
            key = request_key(event)
            if key is None:
                continue
            row = rows.setdefault(key, {"id": key, "owner": key.split(":")[0], "request_id": event.get("request_id"),
                                       "game_seconds": None, "plan_id": None, "execution_plan_id": None,
                                       "status": "pending", "description": "", "action_id": None,
                                       "latency_ms": None, "input_tokens": None, "output_tokens": None,
                                       "cost_usd": None, "orders_submitted": 0, "records": []})
            row["records"].append({"line": line_number, **event})
            if kind in {"request", "planner_request"}:
                if "request" in row:
                    issues.append({"line": line_number, "code": "duplicate_request", "detail": key})
                row["request"] = event
                row["game_seconds"] = event.get("game_loop", 0) / 22.4
                row["model"] = event.get("model", event.get("payload", {}).get("model"))
                plan = event.get("payload", {}).get("state", {}).get("strategic_plan") or {}
                row["plan_id"] = event.get("plan_id", plan.get("plan_id"))
            elif kind in {"response", "planner_response", "plan_accepted"}:
                if "response" in row and kind != "plan_accepted":
                    issues.append({"line": line_number, "code": "duplicate_response", "detail": key})
                row["response"] = event
                row["latency_ms"] = event.get("latency_ms", row["latency_ms"])
                usage = event.get("usage", {})
                row["input_tokens"], row["output_tokens"] = usage.get("input_tokens"), usage.get("output_tokens")
                row["model"] = event.get("model") or row.get("model")
                if row["owner"] == "jev":
                    row["cost_usd"] = estimate_cost(row.get("model"), usage, metrics.pricing)
                    answer = event.get("answer", {})
                    row["action_id"] = answer.get("choice")
                    criteria = row.get("request", {}).get("payload", {}).get("questions", {}).get("next_macro_action", {}).get("criteria", {})
                    row["description"] = criteria.get(str(row["action_id"]), "")
                    if criteria and str(row["action_id"]) not in criteria:
                        issues.append({"line": line_number, "code": "choice_outside_candidates", "detail": key})
                else:
                    row["description"] = event.get("plan", {}).get("objective", "")
                row["status"] = "stale" if event.get("stale") else "response_received"
                if kind == "plan_accepted":
                    row["status"] = "plan_accepted"
                    row["plan_id"] = event["plan"]["plan_id"]
            elif kind == "action":
                if "action" in row:
                    issues.append({"line": line_number, "code": "duplicate_execution", "detail": key})
                row["action"] = event
                if row["action_id"] is not None and str(row["action_id"]) != str(event.get("action_id")):
                    issues.append({"line": line_number, "code": "execution_choice_mismatch", "detail": key})
                row.update(action_id=event.get("action_id"), description=event.get("description", row["description"]),
                           orders_submitted=event.get("orders_submitted", 0), execution_plan_id=event.get("execution_plan_id"))
                row["status"] = event.get("status") or ("wait" if event.get("action_id") == 71 else
                                                       "orders_submitted" if event.get("orders_submitted") else "executor_no_order")
            elif kind in {"api_error", "planner_error", "action_discarded", "decision_discarded", "planner_stale",
                          "request_cancelled", "planner_cancelled", "planner_discarded"}:
                row["status"] = kind
                row["reason"] = event.get("reason", event.get("error", ""))
                row["execution_plan_id"] = event.get("execution_plan_id")
    for row in rows.values():
        if "request" not in row:
            issues.append({"code": "missing_request", "detail": row["id"]})
        # Records contain the full payload exactly once; avoid duplicating it in the HTML.
        for field in ("request", "response", "action"):
            row.pop(field, None)
    result = summary.get("result", "unknown")
    status = summary.get("status") or ("interrupted" if result == "interrupted" else "completed" if events["end"] else "incomplete")
    return {"report_version": 1, "run_id": run_id or directory.name, "settings": settings,
            "summary": summary, "status": status, "game_seconds": metrics.game_loop / 22.4,
            "metrics": metrics.snapshot(), "decisions": list(rows.values()), "heartbeats": heartbeats,
            "plans": plans, "issues": issues, "legacy": legacy,
            "pricing_basis": "recorded_at_run_start" if "pricing" in settings else "current_report_table_2026-09-21"}


def build_report(directory, output_dir=None):
    directory = Path(directory).resolve()
    target = Path(output_dir).resolve() if output_dir else directory
    target.mkdir(parents=True, exist_ok=True)
    report = analyze_run(directory)
    report["artifacts"] = {name: Path(os.path.relpath(directory / name, target)).as_posix()
                           for name in ("events.jsonl", "summary.json", "timeline.log", "engine.log", "game.SC2Replay")
                           if (directory / name).is_file()}
    atomic_json(target / "report.json", report)
    fields = ["id", "owner", "request_id", "game_seconds", "plan_id", "execution_plan_id", "model", "status",
              "action_id", "description", "latency_ms", "input_tokens", "output_tokens", "cost_usd", "orders_submitted", "reason"]
    with (target / "decisions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in report["decisions"]:
            # Model-generated text must not become spreadsheet formulas.
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                             for k, v in row.items() if k in fields})
    data = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = Path(__file__).with_name("utils") / "run_report.html"
    page = template.read_text(encoding="utf-8").replace("__RUN_TITLE__", html.escape(report["run_id"]))
    page = page.replace("__REPORT_DATA__", data)
    (target / "report.html").write_text(page, encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, help="Write derived reports elsewhere; original events are never changed")
    args = parser.parse_args()
    report = build_report(args.run_dir, args.output_dir)
    print(f"Report: {(args.output_dir or args.run_dir).resolve() / 'report.html'}")
    print(f"Decisions: {len(report['decisions'])}; integrity issues: {len(report['issues'])}; "
          f"known Jev estimate: ${report['metrics']['known_cost_usd']:.6f}")


if __name__ == "__main__":
    main()
