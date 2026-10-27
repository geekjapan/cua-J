from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import benchmark_native as benchmark


class BenchmarkNativeTest(unittest.TestCase):
    def args(self):
        return argparse.Namespace(harness="appkit", density=12, openai_model="gpt-6-luna", timeout=0.1, driver_socket="/tmp/owned-driver.sock")

    def state(self, **changes):
        return {"schema": benchmark.HARNESSES["appkit"].state_schema, "pid": 701,
                "counter": 0, "size": "none", "agreed": False, "note_saved": None,
                "density": 12, "distractor_actions": 0, **changes}

    def test_five_rotated_rounds_schedule_all_45_fresh_trials(self):
        schedule = benchmark.trial_schedule(list(benchmark.PROVIDERS), list(benchmark.TASKS), 5)
        self.assertEqual(len(schedule), 45)
        self.assertEqual([trial.order for trial in schedule], list(range(1, 46)))
        for task in benchmark.TASKS:
            for provider in benchmark.PROVIDERS:
                self.assertEqual(sum(trial.task == task and trial.provider == provider for trial in schedule), 5)
        for repetition in range(5):
            self.assertEqual(schedule[repetition * 9].provider, benchmark.PROVIDERS[repetition % 3])
        self.assertEqual([trial.provider for trial in schedule[:6]], list(benchmark.PROVIDERS) * 2)

    def run_fake_trial(self, final=None, events=None, returncode=0, timeout=False, initial=None):
        fixture = mock.Mock(pid=701)
        runner = mock.Mock(pid=702)
        fixture.poll.return_value = None
        runner.wait.side_effect = subprocess.TimeoutExpired("runner", .1) if timeout else None
        runner.wait.return_value = returncode
        initial = self.state() if initial is None else initial
        final = self.state(counter=3) if final is None else final
        states = iter([initial, final])
        events = [{"event": "step", "tool": "click", "candidate": "ax:button:increment",
                   "expected_ids": ["ax:button:increment"], "act_ms": 1,
                   "observation": {"observe_ms": 2}, "oracle_ms": 3,
                   "inference": {"elapsed_ms": 4, "request_count": 1, "total_tokens": 8}},
                  {"event": "outcome", "outcome": "verified"}] if events is None else events
        processes = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            def popen(command, **kwargs):
                processes.append((command, kwargs))
                if len(processes) == 1:
                    state = Path(kwargs["env"][benchmark.HARNESSES["appkit"].state_env])
                    state.touch()
                    return fixture
                log = Path(command[command.index("--log") + 1])
                log.write_text("\n".join(json.dumps(event) for event in events))
                return runner
            with mock.patch.object(benchmark.subprocess, "Popen", side_effect=popen), \
                    mock.patch.object(benchmark, "read_state", side_effect=lambda *args: next(states)), \
                    mock.patch.object(benchmark, "stop_process_group") as cleanup:
                result = benchmark.run_trial(benchmark.Trial("counter", "standard-llm", 1, 1), self.args(), output)
            recorded = json.loads(next(output.glob("*/result.json")).read_text())
            return result, recorded, processes, cleanup

    def test_success_requires_independent_state_and_runner_checks(self):
        result, recorded, processes, cleanup = self.run_fake_trial()
        self.assertTrue(result.verified)
        self.assertEqual(result.status, "success")
        self.assertTrue(all(recorded["oracle_checks"].values()))
        self.assertEqual(recorded["phase_ms"], {"observe": 2, "action": 1, "oracle": 3, "inference": 4, "decide": None})
        self.assertIn("--benchmark-input", processes[1][0])
        self.assertIn("--driver-socket", processes[1][0])
        self.assertIn("/tmp/owned-driver.sock", processes[1][0])
        self.assertTrue(all(kwargs["start_new_session"] for _, kwargs in processes))
        self.assertEqual(cleanup.call_count, 2)

    def test_distractor_and_counter_overshoot_and_exit_failure_cannot_pass(self):
        for final, returncode in ((self.state(counter=4), 0), (self.state(counter=3, distractor_actions=1), 0), (self.state(counter=3), 1)):
            with self.subTest(final=final, returncode=returncode):
                result, *_ = self.run_fake_trial(final=final, returncode=returncode)
                self.assertFalse(result.verified)
                self.assertEqual(result.status, "oracle_failed")

    def test_no_outcome_abstention_and_timeout_remain_failures(self):
        for events, timeout, status in (([], False, "missing_outcome"), ([{"event": "outcome", "outcome": "abstained"}], False, "abstained"), ([], True, "timeout")):
            with self.subTest(status=status):
                result, recorded, _, cleanup = self.run_fake_trial(events=events, timeout=timeout)
                self.assertEqual(result.status, status)
                self.assertFalse(recorded["verified"])
                self.assertIsNotNone(result.end_to_end_ms)
                self.assertGreaterEqual(cleanup.call_count, 2)

    def test_unclear_fixture_never_starts_runner(self):
        result, recorded, processes, cleanup = self.run_fake_trial(initial=self.state(counter=1))
        self.assertEqual(len(processes), 1)
        self.assertEqual(result.status, "exception")
        self.assertFalse(recorded["verified"])
        self.assertIsNone(result.end_to_end_ms)
        self.assertTrue(any(call.args[0] is not None for call in cleanup.call_args_list))

    def test_wrong_fixture_identity_never_starts_runner_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            fixture = mock.Mock(pid=701)
            def popen(command, **kwargs):
                Path(kwargs["env"][benchmark.HARNESSES["appkit"].state_env]).write_text(json.dumps(self.state(pid=99)))
                return fixture
            with mock.patch.object(benchmark.subprocess, "Popen", side_effect=popen) as launch, \
                    mock.patch.object(benchmark, "stop_process_group") as cleanup:
                result = benchmark.run_trial(benchmark.Trial("counter", "standard-llm", 1, 1), self.args(), output)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(result.status, "exception")
            self.assertEqual(result.errors, {"RuntimeError": 1})
            self.assertTrue(next(output.glob("*/result.json")).is_file())
            cleanup.assert_any_call(fixture)

    def test_failure_before_any_log_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(benchmark.subprocess, "Popen", side_effect=FileNotFoundError), \
                    mock.patch.object(benchmark, "stop_process_group"):
                result = benchmark.run_trial(benchmark.Trial("save-note", "openai-decisions", 1, 1), self.args(), output)
            recorded = json.loads(next(output.glob("*/result.json")).read_text())
            self.assertFalse(result.verified)
            self.assertEqual(recorded["errors"], {"FileNotFoundError": 1})

    def test_error_work_usage_accounting(self):
        measurement = benchmark.event_measurements([
            {"event": "step", "tool": "click", "candidate": "wrong", "expected_ids": ["right"],
             "action_error": "stale_element_token", "forbidden_attempts": 1},
            {"event": "outcome", "phase": "decide", "error": "ProviderError",
             "inference": {"elapsed_ms": 10, "request_count": 1, "error_code": "http_429"}},
        ])
        self.assertEqual(measurement["counts"]["action_attempts"], 1)
        self.assertEqual(measurement["counts"]["action_deliveries"], 0)
        self.assertEqual(measurement["counts"]["action_errors"], 1)
        self.assertEqual(measurement["counts"]["unexpected_actions"], 1)
        self.assertEqual(measurement["counts"]["forbidden_attempts"], 1)
        self.assertEqual(measurement["errors"]["http_429"], 1)
        self.assertIsNone(measurement["usage"]["input_tokens"]["sum"])
        self.assertEqual(measurement["usage"]["input_tokens"]["known_requests"], 0)
        self.assertIsNone(benchmark.event_measurements([])["phase_ms"]["inference"])

    def test_medians_ignore_fast_failures_but_denominator_and_wall_times_include_them(self):
        runs = [benchmark.TrialResult("counter", "standard-llm", index, index, status="success", verified=True,
                                      end_to_end_ms=value, wall_ms=value + 5, phase_ms={"inference": value / 2})
                for index, value in enumerate((100, 200, 300), 1)]
        runs.append(benchmark.TrialResult("counter", "standard-llm", 4, 4, status="timeout", wall_ms=1))
        group = benchmark.aggregate(runs)[0]
        self.assertEqual((group["successes"], group["runs"]), (3, 4))
        self.assertEqual(group["successful_end_to_end"], {"n": 3, "median_ms": 200, "min_ms": 100, "max_ms": 300})
        self.assertEqual(group["all_run_wall_ms"], [105, 205, 305, 1])
        self.assertIsNone(group["usage"]["total_tokens"]["sum"])
        self.assertIsNone(group["counts"]["unexpected_actions"]["sum"])
        with tempfile.TemporaryDirectory() as directory:
            benchmark.save_summary(Path(directory), {"trials": [asdict(run) for run in runs]})
            self.assertIn("inconclusive", (Path(directory) / "summary.md").read_text())

    def test_oracle_poll_events_include_nonterminal_wait_without_double_counting(self):
        result = benchmark.event_measurements([
            {"event": "step", "oracle_ms": 20},
            {"event": "oracle", "oracle_ms": 20, "outcome": "unknown"},
            {"event": "oracle", "oracle_ms": 30, "outcome": "verified"},
        ])
        self.assertEqual(result["phase_ms"]["oracle"], 50)

    def test_partial_inference_time_is_unknown(self):
        result = benchmark.event_measurements([
            {"event": "step", "inference": {"elapsed_ms": 100}},
            {"event": "outcome", "inference": {"error_code": "timeout"}},
        ])
        self.assertIsNone(result["phase_ms"]["inference"])
        self.assertEqual(result["errors"], {"timeout": 1})

    def test_completed_action_time_is_unknown_when_another_attempt_is_unfinished(self):
        events = [
            {"event": "action_attempt", "attempt_id": "one", "tool": "click"},
            {"event": "step", "attempt_id": "one", "tool": "click", "act_ms": 100},
            {"event": "action_attempt", "attempt_id": "two", "tool": "click"},
        ]
        for tail in ([], [{"event": "log_error", "error": "invalid_log"}],
                     [{"event": "outcome", "error": "timeout"}]):
            with self.subTest(tail=tail):
                result = benchmark.event_measurements(events + tail)
                self.assertEqual(result["counts"]["action_attempts"], 2)
                self.assertEqual(result["counts"]["unresolved_deliveries"], 1)
                self.assertIsNone(result["phase_ms"]["action"])

    def test_cleanup_failure_is_recorded_and_cannot_pass(self):
        result, *_ = self.run_fake_trial()
        self.assertTrue(result.verified)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(benchmark.subprocess, "Popen", side_effect=FileNotFoundError), \
                    mock.patch.object(benchmark, "stop_process_group", side_effect=RuntimeError):
                result = benchmark.run_trial(benchmark.Trial("counter", "standard-llm", 1, 1), self.args(), Path(directory))
            self.assertEqual(result.status, "cleanup_failed")
            self.assertFalse(result.verified)
            self.assertTrue(next(Path(directory).glob("*/result.json")).is_file())

    def test_main_continues_after_failure_and_rejects_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            def run(trial, args, directory):
                return benchmark.TrialResult(**asdict(trial), status="exception" if trial.order == 1 else "success", verified=trial.order != 1)
            argv = ["--output-dir", str(output), "--repetitions", "1", "--task", "counter"]
            with mock.patch.object(benchmark.sys, "platform", "darwin"), \
                    mock.patch.object(benchmark, "metadata", return_value={}), \
                    mock.patch.object(benchmark, "run_trial", side_effect=run) as execute, \
                    mock.patch("builtins.print"):
                self.assertEqual(benchmark.main(argv), 1)
                self.assertEqual(execute.call_count, 3)
                summary = json.loads((output / "summary.json").read_text())
                self.assertTrue(summary["complete"])
                self.assertEqual(len(summary["trials"]), 3)
                with self.assertRaises(FileExistsError):
                    benchmark.main(argv)

    def test_metadata_records_configuration_without_auth_values(self):
        args = self.args()
        args.output_dir = Path("/tmp/new-evidence")
        rust = benchmark.REPO / "libs/cua-driver/rust"
        with mock.patch.dict("os.environ", {"AZURE_ENDPOINT": "https://secret:password@resource.example/path?key=secret", "AZURE_API_KEY": "private-key", "AZURE_DEPLOYMENT_NAME": "decision-1"}), \
                mock.patch.object(benchmark.subprocess, "check_output", return_value="revision"), \
                mock.patch.object(benchmark.platform, "platform", return_value="test-os"), \
                mock.patch.object(Path, "rglob", return_value=[rust / "src/lib.rs", rust / "target/debug/build/generated.rs"]), \
                mock.patch.object(benchmark, "file_hash", return_value="sha256"):
            metadata = benchmark.metadata(args)
        text = json.dumps(metadata)
        self.assertNotIn("secret", text)
        self.assertNotIn("password", text)
        self.assertNotIn("private-key", text)
        self.assertEqual(metadata["provider_configuration"]["microsoft-decision1"]["resource_host"], "resource.example")
        manifest = metadata["source_sha256"]
        for path in ("verify_native.py", "pyproject.toml", "uv.lock"):
            self.assertIn(str((benchmark.BASE / path).relative_to(benchmark.REPO)), manifest)
        for path in ("main.swift", "SnapshotPublication.swift"):
            self.assertIn("libs/cua-driver/tests/fixtures/apps/macos/appkit/" + path, manifest)
        self.assertIn("libs/cua-driver/rust/src/lib.rs", manifest)
        self.assertNotIn("libs/cua-driver/rust/target/debug/build/generated.rs", manifest)
        self.assertFalse(any(Path(path).name.startswith(".env") for path in manifest))

    def test_note_evidence_redacts_wrong_values_too(self):
        state = self.state(note_saved="private wrong value")
        evidence = benchmark.state_evidence(state)
        self.assertEqual(evidence["note_saved"], "[note text]")
        self.assertNotIn("private", json.dumps(evidence))
        self.assertFalse(benchmark.EXPECTED["save-note"](state))

    @unittest.skipUnless(hasattr(__import__("os"), "killpg"), "POSIX process-group test")
    def test_cleanup_reaps_owned_local_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            benchmark.stop_process_group(process)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_attempt_without_completion_is_unknown_and_not_counted_twice(self):
        attempt = {"event": "action_attempt", "attempt_id": "one", "step": 1,
                   "tool": "click", "candidate": "wrong", "expected_ids": ["right"]}
        partial = benchmark.event_measurements([attempt])
        self.assertEqual(partial["counts"]["action_attempts"], 1)
        self.assertEqual(partial["counts"]["unresolved_deliveries"], 1)
        self.assertIsNone(partial["counts"]["action_deliveries"])
        self.assertIsNone(benchmark.event_measurements([])["counts"]["action_attempts"])
        done = benchmark.event_measurements([attempt,
            {**attempt, "event": "outcome", "phase": "action", "outcome": "unknown",
             "cancelled": True, "delivery_status": "unresolved", "act_ms": 4,
             "observation": {"observe_ms": 123}, "decide_ms": 2}])
        self.assertEqual(done["counts"]["action_attempts"], 1)
        self.assertEqual(done["counts"]["cancelled_actions"], 1)
        self.assertEqual(done["counts"]["unresolved_deliveries"], 1)
        self.assertIsNone(done["counts"]["unexpected_deliveries"])
        self.assertEqual(done["phase_ms"]["observe"], 123)
        self.assertEqual(done["phase_ms"]["action"], 4)

    def test_partial_json_keeps_attempt_evidence_and_unknown_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "runner.jsonl"
            log.write_text(json.dumps({"event": "action_attempt", "attempt_id": "one", "tool": "click",
                "observation": {"observe_ms": 123}, "decide_ms": 5,
                "inference": {"request_count": 1, "elapsed_ms": 4}}) + '\n{"event":')
            result = benchmark.event_measurements(benchmark.read_events(log))
        self.assertEqual(result["counts"]["action_attempts"], 1)
        self.assertEqual(result["counts"]["unresolved_deliveries"], 1)
        self.assertIsNone(result["counts"]["action_deliveries"])
        self.assertEqual(result["phase_ms"]["observe"], 123)
        self.assertEqual(result["counts"]["request_count"], 1)
        self.assertEqual(result["errors"], {"invalid_log": 1})

    def test_oracle_reconciliation_counts_one_proven_delivery(self):
        measurement = benchmark.event_measurements([
            {"event": "action_attempt", "attempt_id": "one", "tool": "click"},
            {"event": "step", "attempt_id": "one", "tool": "click", "delivery_status": "uncertain"},
            {"event": "action_reconciled", "attempt_id": "one"},
            {"event": "outcome", "outcome": "verified"},
        ])
        self.assertEqual(measurement["counts"]["action_attempts"], 1)
        self.assertEqual(measurement["counts"]["action_deliveries"], 1)
        self.assertEqual(measurement["counts"]["unresolved_deliveries"], 0)

    @unittest.skipUnless(hasattr(os, "killpg"), "POSIX process-group test")
    def test_sigterm_controller_persists_interruption_and_reaps_owned_children(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            fixture = work / "fixture"
            fixture.write_text("#!" + sys.executable + "\nimport json,os,time\n"
                "from pathlib import Path\n"
                "Path(os.environ['TEST_STATE']).write_text(json.dumps({"
                "'schema':'test','pid':os.getpid(),'counter':0,'size':'none','agreed':False,"
                "'note_saved':None,'density':12,'distractor_actions':0}))\ntime.sleep(60)\n")
            fixture.chmod(0o700)
            pidfile = work / "owned-pids"
            output = work / "results"
            script = work / "controller.py"
            script.write_text(f"""import sys, json, subprocess
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(Path(benchmark.__file__).parent)!r})
import benchmark_native as b
b.HARNESSES['appkit'] = SimpleNamespace(app=Path({str(work)!r}), executable='fixture', state_env='TEST_STATE', density_env='TEST_DENSITY', state_schema='test')
b.read_state = lambda harness, path, pid: json.loads(path.read_text())
b.metadata = lambda args: {{}}
real_popen = subprocess.Popen
def popen(command, **kwargs):
    if '--log' in command:
        command = [sys.executable, '-c', 'import time; time.sleep(60)']
    child = real_popen(command, **kwargs)
    with open({str(pidfile)!r}, 'a') as stream:
        stream.write(str(child.pid) + '\\n')
    return child
b.subprocess.Popen = popen
raise SystemExit(b.main(['--output-dir', {str(output)!r}, '--repetitions', '1', '--task', 'counter']))
""")
            controller = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            owned = []
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pidfile.exists():
                        owned = [int(pid) for pid in pidfile.read_text().splitlines()]
                        if len(owned) == 2:
                            break
                    if controller.poll() is not None:
                        self.fail(controller.communicate()[1])
                    time.sleep(.02)
                self.assertEqual(len(owned), 2)
                controller.send_signal(signal.SIGTERM)
                stdout, stderr = controller.communicate(timeout=10)
                self.assertEqual(controller.returncode, 130, stderr)
                trial = json.loads(next(output.glob('*/result.json')).read_text())
                self.assertEqual(trial['status'], 'interrupted')
                self.assertFalse(trial['verified'])
                summary = json.loads((output / 'summary.json').read_text())
                self.assertFalse(summary['complete'])
                self.assertEqual(len(summary['trials']), 1)
                self.assertEqual(summary['groups'][0]['runs'], 1)
                for pid in owned:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if controller.poll() is None:
                    controller.kill()
                    controller.communicate()
                for pid in owned:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_help_needs_no_provider_imports(self):
        completed = subprocess.run([sys.executable, str(Path(benchmark.__file__)), "--help"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--driver-socket", completed.stdout)


if __name__ == "__main__":
    unittest.main()
