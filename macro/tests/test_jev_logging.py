"""Logging regressions: shutdown accounting, correlation, failures and legacy reports."""

import asyncio
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sc2_rl_agent.starcraftenv_test.agent.astra_planner import StrategicPlanner
from sc2_rl_agent.starcraftenv_test.agent.jev_agent import DecisionScheduler
from sc2_rl_agent.starcraftenv_test.run_report import analyze_run, build_report
from sc2_rl_agent.starcraftenv_test.utils.run_logging import RunLog, RunMetrics


def request(owner="jev", rid=1, **extra):
    return {"event": "request" if owner == "jev" else "planner_request", "request_id": rid,
            "game_loop": 224, "model": "jev-1.13.0" if owner == "jev" else "gpt-6-astra",
            "payload": {"model": "jev-1.13.0", "state": {"strategic_plan": {"plan_id": 7}},
                        "questions": {"next_macro_action": {"criteria": {"0": "TRAIN PROBE", "71": "EMPTY ACTION"}}}}, **extra}


def response(owner="jev", rid=1, **extra):
    return {"event": "response" if owner == "jev" else "planner_response", "request_id": rid,
            "game_loop": 228, "model": "jev-1.13.0" if owner == "jev" else "gpt-6-astra",
            "usage": {"input_tokens": 1000, "output_tokens": 20}, "latency_ms": 300,
            "answer": {"choice": "0"}, **extra}


class LoggingTests(unittest.TestCase):
    def test_locked_progress_snapshot_does_not_interrupt_game_or_drop_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            run = RunLog(path)
            with patch('sc2_rl_agent.starcraftenv_test.utils.run_logging.os.replace',
                       side_effect=PermissionError('Windows sharing violation')):
                run('heartbeat', game_loop=224, economy={'mineral': 50})
            self.assertEqual(run.status, 'running')
            self.assertIsNone(run.failure)
            self.assertEqual(run.checkpoint_deferrals, 1)
            self.assertEqual(json.loads((path / 'progress.json').read_text())['game_loop'], 0)
            run('heartbeat', game_loop=448, economy={'mineral': 100})
            self.assertEqual(json.loads((path / 'progress.json').read_text())['game_loop'], 448)
            run.finish(result='test')
            run.close()
            events = [json.loads(line) for line in (path / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(sum(e['event'] == 'heartbeat' for e in events), 2)
            self.assertEqual(sum(e['event'] == 'checkpoint_deferred' for e in events), 1)
            self.assertEqual(json.loads((path / 'summary.json').read_text())['status'], 'completed')

    def test_envelope_checkpoint_and_secret_redaction_in_every_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            run = RunLog(path, {"authorization": "secret-key-123"}, secrets=("secret-key-123",))
            with run.capture_engine():
                print("legacy print secret-key-123")
            run.set_game_loop(224)
            run("heartbeat", economy={"mineral": 50}, nested={"api_key": "other-secret"})
            progress = json.loads((path / "progress.json").read_text())
            self.assertEqual(progress["game_seconds"], 10)
            try:
                raise RuntimeError("Bearer accidentally-exposed; secret-key-123")
            except RuntimeError as exc:
                run.fail(exc)
            run.finish()
            run.close()
            events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
            self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
            self.assertTrue(all(e["timestamp_utc"].endswith("+00:00") for e in events))
            self.assertEqual(json.loads((path / "summary.json").read_text())["status"], "failed")
            for file in path.iterdir():
                content = file.read_text(encoding="utf-8")
                for secret in ("secret-key-123", "other-secret", "accidentally-exposed"):
                    self.assertNotIn(secret, content, file.name)

    def test_model_namespaces_and_accepted_plan_do_not_double_count_usage(self):
        m = RunMetrics()
        for event in (request(), request("astra"), response(), response("astra"),
                      {**response("astra"), "event": "plan_accepted", "plan": {"plan_id": 1}}):
            m.observe(event)
        result = m.snapshot()
        self.assertEqual(result["models"]["astra"]["input_tokens"], 1000)
        self.assertEqual(result["models"]["jev"]["input_tokens"], 1000)
        self.assertAlmostEqual(result["known_cost_usd"], .000042)
        self.assertIsNone(result["total_cost_usd"])
        self.assertIsNone(result["models"]["astra"]["known_cost_usd"])

    def test_unknown_models_and_missing_usage_are_not_priced_as_free(self):
        m = RunMetrics()
        m.observe(request())
        m.observe(response(model="future-model"))
        m.observe(request(rid=2))
        m.observe(response(rid=2, usage={}))
        result = m.snapshot()
        self.assertEqual(result["models"]["jev"]["unpriced_requests"], 2)
        self.assertEqual(result["models"]["jev"]["requests_without_usage"], 1)
        self.assertIsNone(result["total_cost_usd"])

    def test_existing_events_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunLog(Path(tmp))
            run.close()
            original = (Path(tmp) / "events.jsonl").read_bytes()
            with self.assertRaises(FileExistsError):
                RunLog(Path(tmp))
            self.assertEqual(original, (Path(tmp) / "events.jsonl").read_bytes())

    def test_cleanup_error_does_not_hide_original_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = RunLog(Path(tmp))
            run.phase = "launching"
            run.fail(RuntimeError("graphics device unavailable"))
            run.fail(OSError("cleanup error"))
            run.finish()
            run.close()
            summary = json.loads((Path(tmp) / "summary.json").read_text())
            self.assertEqual(summary["failure"]["message"], "graphics device unavailable")
            self.assertEqual(summary["telemetry"]["events"]["run_failure"], 2)

    def test_legacy_report_recovers_partial_log_checks_actions_and_escapes_html_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            dangerous = '=1+2 </script><script>window.BAD=true</script>'
            events = [request(), response(), {"event": "action", "request_id": 1, "game_loop": 232,
                      "action_id": 0, "description": dangerous, "orders_submitted": 1, "execution_plan_id": 8},
                      request(rid=2), response(rid=2, answer={"choice": "64"})]
            original = "\n".join(json.dumps(e) for e in events) + '\n{"event":'
            (path / "events.jsonl").write_text(original, encoding="utf-8")
            report = build_report(path)
            self.assertTrue(report["legacy"])
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["decisions"][0]["plan_id"], 7)
            self.assertEqual(report["decisions"][0]["execution_plan_id"], 8)
            self.assertEqual({i["code"] for i in report["issues"]}, {"choice_outside_candidates", "unreadable_event"})
            self.assertEqual((path / "events.jsonl").read_text(), original)
            self.assertNotIn("<script>window.BAD", (path / "report.html").read_text(encoding="utf-8"))
            with (path / "decisions.csv").open(encoding="utf-8-sig", newline="") as stream:
                self.assertTrue(next(csv.DictReader(stream))["description"].startswith("'=1+2"))

    def test_duplicate_execution_and_orphan_response_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            action = {"event": "action", "request_id": 1, "action_id": 71, "orders_submitted": 0}
            events = [request(), response(), action, action, response(rid=2)]
            (path / "events.jsonl").write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
            issues = {i["code"] for i in analyze_run(path)["issues"]}
            self.assertTrue({"duplicate_execution", "execution_choice_mismatch", "missing_request"} <= issues)


class ShutdownUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_jev_response_is_counted_at_shutdown_but_never_executed(self):
        events = []
        client = SimpleNamespace(payload=lambda s, c: {"state": s},
                                 choose=AsyncMock(return_value={"answer": {"choice": "0"}, "usage": {
                                     "input_tokens": 123, "output_tokens": 4}, "model": "jev-1.13.0"}),
                                 close=AsyncMock())
        scheduler = DecisionScheduler(client, lambda name, **kw: events.append({"event": name, **kw}))
        scheduler.submit(224, {"strategic_plan": {"plan_id": 7}}, {0: "TRAIN PROBE"})
        await scheduler.task
        await scheduler.close()
        await scheduler.close()
        self.assertEqual(scheduler.stats["input_tokens"], 123)
        self.assertEqual(sum(e["event"] == "response" for e in events), 1)
        self.assertEqual(events[-1]["event"], "decision_discarded")
        self.assertEqual(events[-1]["plan_id"], 7)
        self.assertIsNone(scheduler.poll(228))

    async def test_completed_and_stale_astra_usage_is_recorded_without_activation_on_close(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                events, clock = [], [0.0]
                client = SimpleNamespace(model="gpt-6-astra", close=AsyncMock(), plan=AsyncMock(return_value={
                    "model": "gpt-6-astra", "usage": {"input_tokens": 500, "output_tokens": 10},
                    "plan": {"objective": "Test"}}))
                scheduler = StrategicPlanner(client, lambda n, **kw: events.append({"event": n, **kw}),
                                             max_requests=1, max_age=4, clock=lambda: clock[0])
                scheduler.tick(0, {}, {})
                await scheduler.task
                if stale:
                    clock[0] = 10
                    scheduler.tick(224, {}, {})
                await scheduler.close()
                self.assertEqual(scheduler.stats["input_tokens"], 500)
                self.assertIsNone(scheduler.active)
                m = RunMetrics()
                for event in events:
                    m.observe(event)
                self.assertEqual(m.snapshot()["models"]["astra"]["responses"], 1)
                terminal = [e for e in events if e["event"] in {"planner_stale", "planner_discarded"}]
                self.assertEqual(len(terminal), 1)
                self.assertEqual(terminal[0]["event"], "planner_stale" if stale else "planner_discarded")


class RunnerFailureTests(unittest.TestCase):
    def test_startup_failure_still_produces_report_and_error_phase_without_model_calls(self):
        from sc2_rl_agent.starcraftenv_test import run_jev
        from sc2_rl_agent.starcraftenv_test.utils import sc2_runtime
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "failed-run"
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-no-network"}), \
                 patch("sys.argv", ["run_jev", "--output-dir", str(output)]), \
                 patch("sc2.maps.get", return_value=SimpleNamespace(path=Path(tmp) / "test.SC2Map", data=b"test-map")), \
                 patch.object(sc2_runtime, "run_windowed_game", side_effect=RuntimeError("test graphics startup failure")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_jev.main(), 1)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["summary"]["failure"]["phase"], "launching")
            self.assertEqual(report["metrics"]["models"]["jev"]["requests"], 0)
            self.assertTrue((output / "report.html").is_file())
            self.assertIn("test graphics startup failure", (output / "engine.log").read_text())


if __name__ == "__main__":
    unittest.main()
