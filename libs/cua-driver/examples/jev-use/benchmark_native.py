"""Sequential native provider benchmark. Runs isolated fixtures and writes fresh evidence.

Timing comparisons remain inconclusive until a separate profile identifies the limiter
and the operator checks equivalent provider configuration and production tuning.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from verify_native import BASE, REPO, DENSITIES, EXPECTED, HARNESSES, NOTE_TEXT, read_state

PROVIDERS = ("standard-llm", "openai-decisions", "microsoft-decision1")
TASKS = ("counter", "save-note", "choose-size")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")


@dataclass(frozen=True)
class Trial:
    task: str
    provider: str
    trial: int
    order: int


@dataclass
class TrialResult:
    task: str
    provider: str
    trial: int
    order: int
    status: str = "not_started"
    verified: bool = False
    exit_code: int | None = None
    oracle_checks: dict = field(default_factory=dict)
    initial: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    inferences: list = field(default_factory=list)
    phase_ms: dict = field(default_factory=dict)
    fixture_startup_ms: float | None = None
    end_to_end_ms: float | None = None
    wall_ms: float = 0.0


class BenchmarkInterrupted(BaseException):
    pass


def interrupt_benchmark(signum, frame):
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)
    raise BenchmarkInterrupted()


def trial_schedule(providers: list[str], tasks: list[str], repetitions: int) -> list[Trial]:
    trials = []
    for repetition in range(repetitions):
        rotated = providers[repetition % len(providers):] + providers[:repetition % len(providers)]
        for task in tasks:
            for provider in rotated:
                trials.append(Trial(task, provider, repetition + 1, len(trials) + 1))
    return trials


def stop_process_group(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    # The group can outlive its leader, including after a runner exits normally.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def state_evidence(state: dict) -> dict:
    return {key: ("[note text]" if state.get(key) is not None else None) if key == "note_saved" else state.get(key)
            for key in ("counter", "size", "agreed", "note_saved", "density", "distractor_actions")}


def initial_checks(state: dict, density: int) -> dict[str, bool]:
    return {
        "counter_clear": type(state.get("counter")) is int and state["counter"] == 0,
        "size_clear": state.get("size") == "none",
        "agreement_clear": state.get("agreed") is False,
        "note_clear": state.get("note_saved") is None,
        "density_matches": state.get("density") == density,
        "zero_distractor_actions": type(state.get("distractor_actions")) is int and state["distractor_actions"] == 0,
    }


def read_events(path: Path) -> list[dict]:
    events = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("invalid event")
            except ValueError:
                events.append({"event": "log_error", "error": "invalid_log"})
                break
            events.append(event)
    return events


def event_measurements(events: list[dict]) -> dict:
    steps = [event for event in events if event.get("event") == "step"]
    attempts = {event["attempt_id"]: event for event in events
                if event.get("event") == "action_attempt" and event.get("attempt_id")}
    completed = [event for event in events if event.get("tool") and event.get("event") != "action_attempt"]
    actions = list(attempts.values()) + [event for event in completed if event.get("attempt_id") not in attempts]
    completions = {event["attempt_id"]: event for event in completed if event.get("attempt_id")}
    reconciled = {event.get("attempt_id") for event in events if event.get("event") == "action_reconciled"}
    completed = [{**event, "delivery_status": "confirmed"} if event.get("attempt_id") in reconciled else event for event in completed]
    deliveries = [event for event in completed if event.get("delivery_status") == "confirmed"]
    unresolved = [event for event in actions if event.get("attempt_id") in attempts
                  and event["attempt_id"] not in completions]
    complete = (any(event.get("event") == "outcome" for event in events) and not unresolved
                and not any(event.get("event") == "log_error" for event in events))
    delivery_known = complete and all(event.get("delivery_status") == "confirmed" or event.get("action_error") for event in completed)
    def phase_sum(values):
        return sum(values) if values and all(type(value) in (int, float) for value in values) else None
    errors = Counter()
    for event in events:
        for key in ("action_error", "error"):
            if event.get(key):
                errors[str(event[key])] += 1
    evidence_events = [event for event in events if event.get("event") != "action_attempt"
                       or event.get("attempt_id") not in completions]
    inferences = []
    for event in evidence_events:
        inference = event.get("inference")
        if isinstance(inference, dict):
            safe = {key: inference.get(key) for key in (
                "requested_model", "returned_model", "elapsed_ms", "request_count", "request_id", "error_code", *TOKEN_FIELDS
            )}
            inferences.append(safe)
            if safe["error_code"]:
                errors[str(safe["error_code"])] += 1
    usage = {}
    for key in TOKEN_FIELDS:
        known = [entry[key] for entry in inferences if type(entry[key]) is int and entry[key] >= 0]
        usage[key] = {"sum": sum(known) if known else None, "known_requests": len(known), "inference_records": len(inferences)}
    phase_events = [event for event in evidence_events if "observation" in event or "decide_ms" in event or event.get("phase") == "decide"]
    phase = {
        "observe": phase_sum([event.get("observation", {}).get("observe_ms") for event in phase_events]),
        "action": None if unresolved else phase_sum([event.get("act_ms") for event in completed]),
        "oracle": phase_sum([event.get("oracle_ms") for event in events if event.get("event") == "oracle"]
                            or [event["oracle_ms"] for event in events if "oracle_ms" in event]),
        "inference": phase_sum([entry["elapsed_ms"] for entry in inferences]),
        "decide": phase_sum([event.get("decide_ms") for event in phase_events]),
    }
    expected = [event for event in actions if isinstance(event.get("expected_ids"), list)]
    unexpected = [event for event in expected if str(event.get("candidate", "")).removesuffix(":foreground") not in event["expected_ids"]]
    counts = {
        "steps": len(steps) if events else None, "action_attempts": len(actions) if complete or actions else None,
        "action_deliveries": len(deliveries) if delivery_known else None,
        "unresolved_deliveries": len(unresolved) + sum(event.get("delivery_status") in {"uncertain", "unresolved"} for event in completed) if events else None,
        "cancelled_actions": sum(event.get("cancelled") is True for event in completed) if events else None,
        "action_errors": sum(bool(event.get("action_error")) or event.get("phase") == "action" for event in completed) if events else None,
        "unexpected_actions": len(unexpected) if events and len(expected) == len(actions) else None,
        "unexpected_deliveries": sum(event.get("candidate") in {entry.get("candidate") for entry in unexpected} for event in deliveries) if delivery_known and len(expected) == len(actions) else None,
        "forbidden_attempts": sum(event.get("forbidden_attempts", 0) for event in events) if any("forbidden_attempts" in event for event in events) else None,
        "request_count": sum(entry["request_count"] for entry in inferences if type(entry["request_count"]) is int) if inferences and all(type(entry["request_count"]) is int for entry in inferences) else None,
    }
    return {"counts": counts, "errors": dict(errors), "usage": usage, "inferences": inferences, "phase_ms": phase}


def run_trial(trial: Trial, args: argparse.Namespace, output: Path) -> TrialResult:
    result = TrialResult(**asdict(trial))
    started = time.monotonic()
    runner_started = None
    fixture = runner = None
    trial_dir = output / f"{trial.order:03d}-{trial.provider}-{trial.task}"
    trial_dir.mkdir(mode=0o700)
    log = trial_dir / "runner.jsonl"
    harness = HARNESSES[args.harness]
    try:
        with tempfile.TemporaryDirectory(prefix="jev-benchmark-") as work:
            state = Path(work) / "state.json"
            executable = harness.app / harness.executable
            env = {**os.environ, harness.state_env: str(state), harness.density_env: str(args.density)}
            fixture = subprocess.Popen([str(executable)], env=env, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 10
            while True:
                if state.exists():
                    try:
                        initial = read_state(harness, state, fixture.pid)
                    except json.JSONDecodeError:
                        initial = None
                    if initial is not None:
                        break
                if fixture.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("fixture_startup_failed")
                time.sleep(0.05)
            result.fixture_startup_ms = (time.monotonic() - started) * 1000
            result.initial = state_evidence(initial)
            result.oracle_checks = {"initial_" + key: value for key, value in initial_checks(initial, args.density).items()}
            if not all(result.oracle_checks.values()):
                raise RuntimeError("fixture_not_clear")
            command = [sys.executable, "python/run_native.py", "--task", f"{args.harness}-{trial.task}",
                       "--provider", trial.provider, "--pid", str(fixture.pid), "--state-file", str(state),
                       "--note-text", NOTE_TEXT, "--log", str(log), "--benchmark-input",
                       "--openai-model", args.openai_model]
            if args.driver_socket:
                command.extend(["--driver-socket", args.driver_socket])
            runner_started = time.monotonic()
            runner = subprocess.Popen(command, cwd=BASE, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                result.exit_code = runner.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                result.status = "timeout"
                stop_process_group(runner)
                runner = None
            observed = read_state(harness, state, fixture.pid)
            result.observed = state_evidence(observed)
            result.oracle_checks.update({
                "expected_state": bool(EXPECTED[trial.task](observed)),
                "zero_distractor_actions": type(observed.get("distractor_actions")) is int and observed["distractor_actions"] == 0,
                "density_matches": observed.get("density") == args.density,
                "runner_exit_zero": result.exit_code == 0,
            })
    except (BenchmarkInterrupted, KeyboardInterrupt):
        result.status = "interrupted"
        result.errors = {"cancelled": 1}
    except Exception as error:
        result.status = "exception"
        # Exception text can contain process arguments or note content.
        result.errors = {type(error).__name__: 1}
    finally:
        if runner_started is not None:
            result.end_to_end_ms = (time.monotonic() - runner_started) * 1000
        try:
            events = read_events(log)
            if any(event.get("event") == "log_error" for event in events) and result.status == "not_started":
                result.status = "invalid_log"
            measurements = event_measurements(events)
            measurements["errors"] = dict(Counter(measurements["errors"]) + Counter(result.errors))
            for key, value in measurements.items():
                setattr(result, key, value)
            outcomes = [event.get("outcome") for event in events if event.get("event") == "outcome"]
            result.oracle_checks["runner_verified"] = bool(outcomes and outcomes[-1] == "verified")
            result.verified = bool(result.oracle_checks) and all(result.oracle_checks.values()) and result.status == "not_started"
            if result.status == "not_started":
                result.status = "success" if result.verified else str(outcomes[-1] if outcomes else "missing_outcome")
                if result.status == "verified":
                    result.status = "oracle_failed"
        except Exception as error:
            result.status = "invalid_log"
            result.errors[type(error).__name__] = result.errors.get(type(error).__name__, 0) + 1
        finally:
            for process in (runner, fixture):
                try:
                    stop_process_group(process)
                except Exception as error:
                    result.status = "cleanup_failed"
                    result.verified = False
                    result.errors[type(error).__name__] = result.errors.get(type(error).__name__, 0) + 1
            result.wall_ms = (time.monotonic() - started) * 1000
            (trial_dir / "result.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    return result


def distribution(values: list[float]) -> dict:
    return {"n": len(values), "median_ms": statistics.median(values) if values else None,
            "min_ms": min(values) if values else None, "max_ms": max(values) if values else None}


def aggregate(results: list[TrialResult]) -> list[dict]:
    groups = []
    for provider, task in dict.fromkeys((result.provider, result.task) for result in results):
        runs = [result for result in results if result.provider == provider and result.task == task]
        successful = [result for result in runs if result.verified]
        errors = Counter()
        for result in runs:
            errors.update(result.errors)
        counts = {}
        for key in ("steps", "action_attempts", "action_deliveries", "unresolved_deliveries", "cancelled_actions", "action_errors", "unexpected_actions", "unexpected_deliveries", "forbidden_attempts", "request_count"):
            known = [result.counts[key] for result in runs if result.counts.get(key) is not None]
            counts[key] = {"sum": sum(known) if known else None, "known_runs": len(known)}
        usage = {}
        for key in TOKEN_FIELDS:
            records = [result.usage[key] for result in runs if key in result.usage]
            known = [record["sum"] for record in records if record["sum"] is not None]
            usage[key] = {"sum": sum(known) if known else None,
                          "known_requests": sum(record["known_requests"] for record in records),
                          "inference_records": sum(record["inference_records"] for record in records)}
        groups.append({"provider": provider, "task": task, "successes": len(successful), "runs": len(runs),
                       "statuses": dict(Counter(result.status for result in runs)), "errors": dict(errors),
                       "counts": counts, "usage": usage,
                       "oracle_check_passes": {key: sum(result.oracle_checks.get(key) is True for result in runs) for key in sorted({key for result in runs for key in result.oracle_checks})},
                       "successful_inference": distribution([result.phase_ms["inference"] for result in successful if result.phase_ms.get("inference") is not None]),
                       "successful_end_to_end": distribution([result.end_to_end_ms for result in successful if result.end_to_end_ms is not None]),
                       "all_run_wall": distribution([result.wall_ms for result in runs]),
                       "all_run_wall_ms": [result.wall_ms for result in runs]})
    return groups


def file_hash(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None


def metadata(args: argparse.Namespace) -> dict:
    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=REPO, text=True).strip()
    harness = HARNESSES[args.harness]
    driver = shutil.which(os.environ.get("CUA_DRIVER_BIN", "cua-driver"))
    source = REPO / "libs/cua-driver/tests/fixtures/apps/macos/appkit/main.swift"
    versions = {}
    for package in ("httpx", "openai", "mcp", "typesafe-sdk"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    azure = urlsplit(os.environ.get("AZURE_ENDPOINT", ""))
    rust = REPO / "libs/cua-driver/rust"
    source_files = sorted(set(BASE.glob("python/*.py"))
                          | {Path(__file__).resolve(), BASE / "verify_native.py", BASE / "pyproject.toml",
                             BASE / "uv.lock", source, source.with_name("SnapshotPublication.swift")}
                          | {path for path in rust.rglob("*.rs") if "target" not in path.relative_to(rust).parts})
    manifest = {str(path.relative_to(REPO)): file_hash(path) for path in source_files}
    return {"source_sha256": manifest, "source_manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()).hexdigest(), "started_utc": datetime.now(timezone.utc).isoformat(), "repo_head": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "os": platform.platform(), "architecture": platform.machine(), "python": sys.version,
            "package_versions": versions, "cpu_count": os.cpu_count(), "load_average": list(os.getloadavg()),
            "driver_sha256": file_hash(Path(driver) if driver else None),
            "fixture_sha256": file_hash(harness.app / harness.executable), "fixture_source_sha256": file_hash(source),
            "arguments": {**vars(args), "output_dir": str(args.output_dir)},
            "input_mode": "benchmark-input", "baseline_reasoning": "none",
            "note_text": "[public synthetic note]", "profile": "not collected; run separately",
            "provider_configuration": {
                "standard-llm": {"endpoint": "https://api.openai.com/v1/responses", "model": args.openai_model, "reasoning": "none"},
                "openai-decisions": {"endpoint": "https://api.openai.com/v1/decisions", "model": args.openai_model},
                "microsoft-decision1": {"resource_host": azure.hostname, "deployment": os.environ.get("AZURE_DEPLOYMENT_NAME", "decision-1")},
                "http_timeout_s": 60, "automatic_retries": 0, "redirects": False,
                "min_probability": 0.6, "min_margin": 0.1, "max_observation_age_ms": 30000}}


def save_summary(output: Path, data: dict) -> None:
    data["groups"] = aggregate([TrialResult(**trial) for trial in data["trials"]])
    (output / "summary.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    rows = ["# Native provider benchmark", "", "Verdict: inconclusive. No automatic winner.", "",
            "A separate limiter profile and equivalent tuning checks are required. Protocol and model may be confounded. These are bounded synthetic tasks.", "",
            "Times below cover successful runs only. Every attempted run remains in the success denominator. Unknown usage remains null.", "",
            "| Provider | Task | Success | Inference median / range ms | End-to-end median / range ms |", "| --- | --- | --- | --- | --- |"]
    for group in data["groups"]:
        times = ["unknown" if group[key]["n"] == 0 else f'{group[key]["median_ms"]:.1f} / {group[key]["min_ms"]:.1f}-{group[key]["max_ms"]:.1f}'
                 for key in ("successful_inference", "successful_end_to_end")]
        rows.append(f'| {group["provider"]} | {group["task"]} | {group["successes"]}/{group["runs"]} | {times[0]} | {times[1]} |')
    rows.extend(["", "See summary.json for errors, action counts, nullable token coverage, phase times, all-run wall times, and provenance.", ""])
    (output / "summary.md").write_text("\n".join(rows), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("appkit",), default="appkit")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--providers", nargs="+", choices=PROVIDERS, default=list(PROVIDERS))
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--density", type=int, choices=DENSITIES, default=12)
    parser.add_argument("--openai-model", default="gpt-6-luna")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--driver-socket", help="socket of the separately owned isolated Driver daemon")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.repetitions < 1 or args.timeout <= 0 or len(set(args.providers)) != len(args.providers):
        parser.error("repetitions and timeout must be positive; providers must be unique")
    if sys.platform != "darwin":
        parser.error("the AppKit benchmark requires macOS")
    output = args.output_dir.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    data = {"complete": False, "verdict": "inconclusive", "metadata": metadata(args), "trials": []}
    previous_handlers = {sig: signal.signal(sig, interrupt_benchmark) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for trial in trial_schedule(args.providers, list(dict.fromkeys(args.task or TASKS)), args.repetitions):
            result = run_trial(trial, args, output)
            data["trials"].append(asdict(result))
            save_summary(output, data)
            print(json.dumps({"order": trial.order, "provider": trial.provider, "task": trial.task, "status": result.status}), flush=True)
            if result.status == "interrupted":
                return 130
        data["complete"] = True
    except (BenchmarkInterrupted, KeyboardInterrupt):
        return 130
    finally:
        save_summary(output, data)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 0 if all(trial["verified"] for trial in data["trials"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
