"""Versioned run events, cost accounting and durable live checkpoints (stdlib only)."""

import contextlib
import io
import json
import math
import os
import re
import time
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2
PRICING = {
    "jev-1.13.0": {"input_usd_per_million": 0.042, "output_usd_per_million": 0.0,
                   "checked_on": "2026-09-21", "source": "https://docs.typesafe.ai/models"},
}
SECRET_FIELDS = {"authorization", "api_key", "apikey", "access_token", "refresh_token", "password", "secret"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)


def component(event):
    if event.startswith(("planner_", "plan_")):
        return "astra"
    if event in {"request", "response", "api_error", "request_cancelled", "decision_discarded", "action_mask"}:
        return "jev"
    if event in {"action", "action_discarded", "army_intent_changed", "order_lifecycle", "engine_action_error",
                 "priority_order_ready", "priority_order_delayed", "priority_order_acknowledged",
                 "army_target_changed", "scout_target_changed", "enemy_location_cleared", "engine_action_response_unconfirmed"}:
        return "executor"
    return "runtime"


def request_key(event):
    rid = event.get("request_id")
    if rid is None and event.get("event") == "plan_accepted":
        rid = event.get("plan", {}).get("plan_id")
    if rid is None:
        return None
    owner = "astra" if component(event["event"]) == "astra" else "jev"
    return f"{owner}:{rid}"


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    def at(q):
        index = (len(ordered) - 1) * q
        low, high = math.floor(index), math.ceil(index)
        return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 3)
    return {"count": len(ordered), "p50": at(.5), "p95": at(.95), "p99": at(.99), "max": round(ordered[-1], 3)}


def estimate_cost(model, usage, pricing):
    rate = pricing.get(model)
    if not rate or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")):
        return None
    return (usage["input_tokens"] * rate["input_usd_per_million"]
            + usage["output_tokens"] * rate["output_usd_per_million"]) / 1_000_000


class RunMetrics:
    """Same aggregation for live checkpoints and offline/legacy reports."""

    def __init__(self, pricing=None):
        self.pricing = PRICING if pricing is None else pricing
        self.events = Counter()
        self.models = {name: Counter() for name in ("jev", "astra")}
        self.latencies = {name: [] for name in self.models}
        self.seen_usage = set()
        self.model_names = {}
        self.known_cost = 0.0
        self.game_loop = 0

    def observe(self, event):
        kind = event["event"]
        self.events[kind] += 1
        self.game_loop = max(self.game_loop, event.get("game_loop", 0) or 0)
        owner = "astra" if component(kind) == "astra" else "jev"
        counts = self.models[owner]
        if kind in ("request", "planner_request"):
            counts["requests"] += 1
            model = event.get("model", event.get("payload", {}).get("model"))
            if model:
                self.model_names[owner] = model
        if kind in ("api_error", "planner_error"):
            counts["errors"] += 1
        if kind in ("request_cancelled", "planner_cancelled"):
            counts["cancelled"] += 1
        if kind in ("response", "planner_response", "plan_accepted"):
            key = request_key(event)
            # v1 recorded Astra usage on plan_accepted; v2 also records all responses.
            if key in self.seen_usage:
                return
            self.seen_usage.add(key)
            counts["responses"] += 1
            usage = event.get("usage", {})
            for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"):
                value = usage.get(field)
                if type(value) is int and value >= 0:
                    counts[field] += value
            if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens")):
                counts["responses_with_usage"] += 1
            latency = event.get("latency_ms")
            if isinstance(latency, (int, float)) and math.isfinite(latency):
                self.latencies[owner].append(latency)
            model = event.get("model") or self.model_names.get(owner)
            if model:
                self.model_names[owner] = model
            cost = estimate_cost(model, usage, self.pricing) if owner == "jev" else None
            if cost is not None:
                counts["priced_responses"] += 1
                counts["known_cost_usd"] += cost
                self.known_cost += cost

    def snapshot(self):
        models = {}
        for owner, counts in self.models.items():
            values = {k: counts[k] for k in ("requests", "responses", "errors", "cancelled", "input_tokens",
                      "output_tokens", "cached_input_tokens", "reasoning_output_tokens", "responses_with_usage", "priced_responses")}
            values.update(model=self.model_names.get(owner), latency_ms=percentiles(self.latencies[owner]),
                          requests_without_usage=max(0, counts["requests"] - counts["responses_with_usage"]),
                          unpriced_requests=max(0, counts["requests"] - counts["priced_responses"]),
                          known_cost_usd=round(counts["known_cost_usd"], 9) if owner == "jev" else None,
                          billing="input_token_estimate" if owner == "jev" else "codex_account_not_priced")
            models[owner] = values
        complete = all(c["requests"] == c["priced_responses"] for c in self.models.values())
        return {"models": models, "events": dict(self.events), "known_cost_usd": round(self.known_cost, 9),
                "cost_complete": complete, "total_cost_usd": round(self.known_cost, 9) if complete else None,
                "cost_note": "List-price estimate, not an invoice. Missing/cancelled usage and Codex account cost are unknown."}


class EngineStream(io.TextIOBase):
    """Capture legacy prints without echoing them over the concise progress console."""

    def __init__(self, run, name):
        self.run, self.name, self.pending = run, name, ""

    @property
    def encoding(self):
        return "utf-8"

    def write(self, text):
        if self.run.engine.closed:
            return len(text)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.run.engine.write(f"{utc_now()} [{self.name}] {self.run.clean(line)}\n")
        return len(text)

    def flush(self):
        if self.run.engine.closed:
            self.pending = ""
            return
        if self.pending:
            self.run.engine.write(f"{utc_now()} [{self.name}] {self.run.clean(self.pending)}\n")
            self.pending = ""
        self.run.engine.flush()


class RunLog:
    def __init__(self, directory, settings=None, secrets=(), console=None):
        self.directory = Path(directory)
        self.console = console
        self.secrets = tuple(s for s in secrets if s)
        self.run_id = self.directory.name + "-" + uuid.uuid4().hex[:8]
        self.started = time.monotonic()
        self.game_loop, self.seq = 0, 0
        self.phase, self.status = "setup", "running"
        self.failure, self.summary = None, {}
        self.checkpoint_deferrals = 0
        self.closed = False
        self.metrics = RunMetrics()
        self.file = (self.directory / "events.jsonl").open("x", encoding="utf-8", buffering=1)
        self.timeline = (self.directory / "timeline.log").open("x", encoding="utf-8", buffering=1)
        self.engine = (self.directory / "engine.log").open("x", encoding="utf-8", buffering=1)
        config = self.clean(settings or {})
        config.update(run_id=self.run_id, schema_version=SCHEMA_VERSION, started_at_utc=utc_now(), pricing=PRICING)
        atomic_json(self.directory / "run.json", config)
        self("run_start", settings=config)

    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if str(k).lower().replace("-", "_") in SECRET_FIELDS else self.clean(v)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
        return value

    def set_game_loop(self, game_loop):
        self.game_loop = int(game_loop)

    def __call__(self, event, **fields):
        if self.closed:
            raise RuntimeError("Cannot append to a closed run log")
        self.seq += 1
        if "game_loop" in fields:
            self.set_game_loop(fields["game_loop"])
        if event == "start":
            self.phase = "playing"
        elif event == "sc2_process_started":
            self.phase = "launching"
        warning = event in {"action_discarded", "planner_stale", "request_cancelled", "planner_cancelled",
                            "decision_discarded", "planner_discarded", "planner_budget_exhausted", "checkpoint_deferred"}
        if event == "action" and fields.get("status") == "executor_no_order":
            warning = True
        level = "error" if event.endswith("error") or event == "run_failure" else "warning" if warning else "info"
        record = self.clean({**fields, "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                            "seq": self.seq, "timestamp_utc": utc_now(), "wall_seconds": round(time.monotonic() - self.started, 6),
                            "game_loop": self.game_loop, "game_seconds": round(self.game_loop / 22.4, 6),
                            "component": component(event), "level": level, "event": event})
        key = request_key(record)
        if key:
            record["trace_id"] = f"{self.run_id}/{key}"
        self.file.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
        self.metrics.observe(record)
        message = self._message(record)
        if message:
            line = f"{record['timestamp_utc']} game={record['game_seconds']:07.2f}s {level.upper()} {message}\n"
            self.timeline.write(line)
            if self.console and (event in {"heartbeat", "plan_accepted", "run_start", "run_end", "start"} or level != "info"):
                self.console.write(line)
                self.console.flush()
        if event in {"run_start", "heartbeat", "plan_accepted", "run_failure", "end", "run_end"}:
            self.checkpoint()

    def _message(self, e):
        kind = e["event"]
        if kind == "heartbeat":
            r = e.get("economy", {})
            m = self.metrics.snapshot()["models"]
            return (f"workers={r.get('worker_supply', '?')} army={r.get('army_supply', '?')} "
                    f"minerals={r.get('mineral', '?')} gas={r.get('gas', '?')} "
                    f"Jev={m['jev']['responses']}/{m['jev']['requests']} "
                    f"Astra={m['astra']['responses']}/{m['astra']['requests']} Jev_est=${m['jev']['known_cost_usd']:.6f}")
        if kind == "action" and e.get("status") != "wait":
            return f"Jev#{e.get('request_id')} {e.get('description')} {e.get('status')} orders={e.get('orders_submitted', 0)}"
        if kind == "plan_accepted":
            p = e["plan"]
            return f"Astra#{p['plan_id']} {p.get('objective')} | {p.get('guidance')}"
        if kind == "planner_trigger":
            return f"Astra refresh queued: {e.get('reason')}"
        if e["level"] != "info":
            return f"{kind} {request_key(e) or ''} {e.get('error', e.get('reason', ''))}"
        if kind in {"run_start", "start", "run_end", "sc2_process_started", "sc2_connected", "army_intent_changed", "plan_expired", "plan_progress"}:
            return kind + " " + str(e.get("result", e.get("intent", e.get("pid", ""))))
        return None

    def checkpoint(self):
        try:
            atomic_json(self.directory / "progress.json", self.snapshot())
        except PermissionError:
            # Windows readers can temporarily deny rename/delete sharing. The
            # append-only event log is already persisted; retry the derived
            # progress snapshot at the next checkpoint without blocking SC2.
            self.checkpoint_deferrals += 1
            self("checkpoint_deferred", reason="progress_file_temporarily_locked",
                 count=self.checkpoint_deferrals)

    def snapshot(self):
        return self.clean({**self.summary, "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                           "updated_at_utc": utc_now(), "status": self.status, "phase": self.phase,
                           "game_loop": self.game_loop, "game_seconds": self.game_loop / 22.4,
                           "wall_seconds": time.monotonic() - self.started, "failure": self.failure,
                           "checkpoint_deferrals": self.checkpoint_deferrals,
                           "telemetry": self.metrics.snapshot()})

    def write_summary(self, summary):
        self.summary.update(summary)
        atomic_json(self.directory / "summary.json", self.snapshot())

    def fail(self, exc):
        failure = self.clean({"type": type(exc).__name__, "message": str(exc), "phase": self.phase})
        if self.failure is None:
            self.failure = failure  # Cleanup errors must not hide the original failure.
        if self.status != "failed":
            self.status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        # No locals or environment values are captured in the traceback.
        trace = self.clean("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        self.engine.write(f"{utc_now()} [exception]\n{trace}\n")
        self("run_failure", error=failure["message"], failure=failure)

    def finish(self, status="completed", result=None):
        if self.status == "running":
            self.status = status
        if result is not None:
            self.summary["result"] = result
        self.phase = "finished"
        self("run_end", status=self.status, result=self.summary.get("result", "unknown"))
        self.write_summary(self.summary)

    @contextlib.contextmanager
    def capture_engine(self):
        stdout, stderr = EngineStream(self, "stdout"), EngineStream(self, "stderr")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                yield
            finally:
                stdout.flush()
                stderr.flush()

    def close(self):
        if not self.closed:
            self.closed = True
            for stream in (self.file, self.timeline, self.engine):
                stream.close()
