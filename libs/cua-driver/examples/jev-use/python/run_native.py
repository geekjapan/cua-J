"""Run one native jev-use task against a desktop window (RFC #4268, Phase 1).

The runner attaches to an already running application (``--pid``), finds the
task's window by title, and loops: read the app-owned oracle, observe the
window once with ``get_window_state`` (tree and screenshot together), build
the closed candidate set, send a validated ``cua.jev_choice_request_v2`` to
the chooser, dispatch the one selected element-bound action, and read the
oracle again. The chooser only ever selects a supplied ID.

Built-in tasks drive the AppKit, WPF, WinUI3, or GTK3 harness launched in task
mode (``CUA_APPKIT_TASK_STATE``, ``CUA_WPF_TASK_STATE``,
``CUA_WINUI3_TASK_STATE``, or ``CUA_GTK3_TASK_STATE`` set to ``<path>``); ``verify_native.py`` launches it, runs this runner, and
checks the state file independently.
"""

from __future__ import annotations

import argparse
import math
from contextlib import ExitStack
from dataclasses import replace

import httpx
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from choose_action import validate_request
from core import Candidate, VisualObservationError, parse_visual_regions, validate_choice
from decision_models import DecisionRequest, TypeSafeDecisionModel, choose
from decision_providers import OpenAIDecisionModel, MicrosoftDecisionModel, ProviderError, provider_transport
from native_dispatch import validate_dispatch
from standard_llm import choose_standard
from driver_env import driver_environment
from jev_adapter import choose_mock_for_task
from native import NativeObservation, NativeObservationError
from native_roles import Platform
from native_tasks import (
    NATIVE_TASK_IDS,
    NativeTask,
    OracleError,
    native_task,
    native_choice_request,
    visual_fallback_reason,
)
from s1_service import S1ServiceError, choose_s1_service, s1_service_url
from run import Driver, DriverToolError, background_refusal_code, supports_capture_bound_click
from sources import NativeAccessibilitySource, VisualRegionSource
from tasks import TaskSources

STALE_TOKEN_CODES = frozenset({"stale_element_token"})
# A partial or truncated tree is observed once more with this walk budget.
REOBSERVE_TIMEOUT_MS = 5_000


def host_platform() -> Platform:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def is_stale_token_error(error: BaseException) -> bool:
    if isinstance(error, DriverToolError) and error.code in STALE_TOKEN_CODES:
        return True
    return "element_token is stale" in str(error)


def write_event(log_path: Path | None, event: dict[str, Any]) -> None:
    line = json.dumps(event, sort_keys=True)
    print(line)
    if log_path:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


async def find_window(driver: Driver, pid: int, title: str) -> dict[str, Any]:
    for _ in range(40):
        windows = (await driver.call("list_windows", {"pid": pid})).get("windows", [])
        matches = [
            window
            for window in windows
            if window.get("title") == title and window.get("is_on_screen") is not False
        ]
        if matches:
            return matches[0]
        await asyncio.sleep(0.25)
    raise RuntimeError(f"window {title!r} of pid {pid} did not appear")


async def observe(
    driver: Driver, task: NativeTask, pid: int, window_id: int, timeout_ms: int | None = None
) -> NativeObservation:
    arguments: dict[str, Any] = {
        "pid": pid,
        "window_id": window_id,
        "include_accessibility_tree": True,
        "include_screenshot": True,
        # The observation needs structured elements, the markdown tree and the
        # completeness flags of the walk; cua-driver 0.35 returns the lean
        # markdown-only read unless asked for the full response.
        "full_output": True,
        **task.scope.window_state_arguments(),
    }
    if timeout_ms is not None:
        arguments["timeout_ms"] = timeout_ms
    try:
        payload = await driver.call("get_window_state", arguments)
    except DriverToolError as error:
        # Drivers before 0.35 always return the full response and refuse the
        # unknown argument.
        if "unknown argument full_output" not in str(error):
            raise
        arguments.pop("full_output")
        payload = await driver.call("get_window_state", arguments)
    return NativeObservation.from_window_state(
        payload, expected_pid=pid, expected_window_id=window_id
    )


async def observe_step(
    driver: Driver,
    task: NativeTask,
    pid: int,
    window_id: int,
    platform: Platform,
    foreground_ids: frozenset[str],
) -> tuple[TaskSources, dict[str, Any]]:
    """Take one observation, reobserving once with a larger walk budget if needed.

    A truncated tree, or a partial tree with no native candidate, is observed
    once more with ``REOBSERVE_TIMEOUT_MS``. The newer observation replaces the
    older one entirely, so every token in the step comes from one snapshot.
    """
    started = time.perf_counter()
    record: dict[str, Any] = {"reobserved": False}

    def build(observation: NativeObservation) -> TaskSources:
        ax = NativeAccessibilitySource.from_observation(
            observation, platform, redact=task.redact_text, text_method=task.text_method
        )
        return TaskSources(ax=ax, foreground_ids=foreground_ids)

    record["observed_at"] = time.monotonic()
    sources = build(await observe(driver, task, pid, window_id))
    observation = sources.ax.observation  # type: ignore[union-attr]
    native_count = sum(
        1 for candidate in task.plan(sources).candidates if candidate.source == "ax"
    )
    if observation.truncated or (observation.partial and native_count == 0):
        record = {"reobserved": True, "reason": "truncated" if observation.truncated else "partial_empty"}
        record["observed_at"] = time.monotonic()
        sources = build(
            await observe(driver, task, pid, window_id, timeout_ms=REOBSERVE_TIMEOUT_MS)
        )
    observation = sources.ax.observation  # type: ignore[union-attr]
    record.update(
        {
            "snapshot_id": observation.snapshot_id,
            "capture_id": observation.capture_id,
            "elements_complete": observation.complete,
            "truncated": observation.truncated,
            "controls": len(sources.ax.controls),  # type: ignore[union-attr]
            "excluded": dict(sources.ax.native.excluded),  # type: ignore[union-attr]
            "observe_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )
    return sources, record


async def maybe_visual(
    driver: Driver,
    task: NativeTask,
    sources: TaskSources,
    available_tools: set[str],
    capture_bound_click: bool,
) -> tuple[TaskSources, dict[str, Any]]:
    """Parse visual regions from the same capture, only under the fallback rule."""
    native_count = sum(1 for c in task.plan(sources).candidates if c.source == "ax")
    reason = visual_fallback_reason(sources, task, native_count)
    if reason is None:
        return sources, {"status": "skipped"}
    if not capture_bound_click or "parse_visual_regions" not in available_tools:
        return sources, {"status": "unavailable", "reason": reason}
    observation = sources.ax.observation  # type: ignore[union-attr]
    started = time.perf_counter()
    try:
        result = await driver.call(
            "parse_visual_regions",
            {
                "capture_id": observation.capture_id,
                "options": {
                    "kinds": ["text", "icon"],
                    "min_confidence": task.visual_min_confidence,
                    "max_regions": 100,
                },
            },
        )
        visual = parse_visual_regions(
            result,
            expected_capture_id=observation.capture_id or "",
            expected_pid=observation.pid,
            expected_window_id=observation.window_id,
        )
    except DriverToolError as error:
        return sources, {"status": "error", "reason": reason, "error_code": error.code or "driver_error"}
    except VisualObservationError as error:
        return sources, {"status": "error", "reason": reason, "error_code": error.code}
    return (
        TaskSources(
            ax=sources.ax,
            visual=VisualRegionSource(
                visual, "background", capture_bound_click, min_confidence=task.visual_min_confidence
            ),
            visual_path=True,
            foreground_ids=sources.foreground_ids,
        ),
        {"status": "ok", "reason": reason, "region_count": len(visual.regions),
         "parse_ms": round((time.perf_counter() - started) * 1000, 2)},
    )


def choose_live(request: dict[str, Any]) -> tuple[str | None, float, dict[str, float]]:
    from typesafe_sdk import TypeSafeClient

    with TypeSafeClient() as client:
        result = choose(
            TypeSafeDecisionModel(client), DecisionRequest.from_validated(validate_request(request))
        )
    if result.kind == "error":
        raise RuntimeError(f"provider decision failed: {result.reason}")
    return result.selected_id, result.confidence or 0.0, dict(result.probabilities)


def assert_in_scope(candidate: Candidate, pid: int, window_id: int) -> None:
    """Refuse any action addressed outside the task's one window."""
    if candidate.tool is None:
        return
    if candidate.arguments.get("pid") != pid or candidate.arguments.get("window_id") != window_id:
        raise RuntimeError("candidate addresses a window outside the task scope")


async def poll_oracle(task: NativeTask, steps: int) -> str:
    outcome = "unknown"
    for _ in range(20):
        outcome = task.classify(task.read_oracle(), steps=steps)
        if outcome in {"verified", "refuted"}:
            return outcome
        await asyncio.sleep(0.1)
    return outcome


NEW_PROVIDERS = ("openai-decisions", "microsoft-decision1", "standard-llm")


async def run_task(args: argparse.Namespace, task: NativeTask) -> str:
    with ExitStack() as stack:
        transport = None
        if args.provider in NEW_PROVIDERS:
            client = stack.enter_context(httpx.Client(timeout=60.0, follow_redirects=False,
                                                       transport=httpx.HTTPTransport(retries=0)))
            transport = provider_transport(args.provider, client, args.openai_model)
        return await _run_task(args, task, transport)


async def _run_task(args: argparse.Namespace, task: NativeTask, transport: Any = None) -> str:
    platform: Platform = args.platform or host_platform()
    log_path = Path(args.log) if args.log else None
    if log_path:
        log_path.write_text("", encoding="utf-8")
    history: list[dict[str, Any]] = []
    foreground_ids: set[str] = set()
    label = f"jev-native-python-{uuid.uuid4().hex[:8]}"
    params = StdioServerParameters(
        command=os.getenv("CUA_DRIVER_BIN", "cua-driver"), args=["mcp", *(["--socket", args.driver_socket] if getattr(args, "driver_socket", None) else [])], env=driver_environment()
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            advertised = (await session.list_tools()).tools
            available_tools = {tool.name for tool in advertised}
            capture_bound_click = supports_capture_bound_click(advertised)
            driver = Driver(session, label)
            window = await find_window(driver, args.pid, task.scope.window_title)
            window_id = int(window["window_id"])
            write_event(
                log_path,
                {"event": "start", "task": task.id, "language": "python", "provider": args.provider,
                 "platform": platform, "pid": args.pid, "window_id": window_id},
            )
            for step in range(1, task.max_steps + 1):
                before = task.read_oracle()
                current = task.classify(before, steps=step - 1)
                if current in {"verified", "refuted"}:
                    write_event(log_path, {"event": "outcome", "outcome": current, "step": step - 1})
                    return current

                sources, observed = await observe_step(
                    driver, task, args.pid, window_id, platform, frozenset(foreground_ids)
                )
                sources, visual_record = await maybe_visual(
                    driver, task, sources, available_tools, capture_bound_click
                )
                plan = task.plan(sources)
                request = native_choice_request(task, sources, plan, history, benchmark_input=getattr(args, "benchmark_input", False))
                validate_request(request)

                decide_started = time.perf_counter()
                inference = None
                decision = None
                if args.provider in NEW_PROVIDERS:
                    model_request = replace(DecisionRequest.from_validated(validate_request(request)),
                        observation_text=task.redact_text(sources.ax.observation.tree_markdown)[:24000])
                    try:
                        if args.provider == "standard-llm":
                            decision = await asyncio.to_thread(choose_standard, transport, model_request, plan.candidates)
                        else:
                            model = OpenAIDecisionModel(transport) if args.provider == "openai-decisions" else MicrosoftDecisionModel(transport)
                            decision = await asyncio.to_thread(choose, model, model_request)
                        inference = decision.inference.to_wire() if decision.inference else None
                        if decision.kind == "error":
                            raise ValueError("decision_error")
                        validate_dispatch(decision, model_request, plan.candidates, sources.ax.observation,
                            pid=args.pid, window_id=window_id, observed_at=observed["observed_at"],
                            max_age_ms=args.max_observation_age_ms, min_probability=args.min_probability,
                            min_margin=args.min_margin, require_probabilities=args.provider != "standard-llm")
                        choice, confidence, probabilities = decision.selected_id, decision.confidence, dict(decision.probabilities)
                    except (ProviderError, ValueError) as error:
                        rejection_reason = str(error)
                        if isinstance(error, ProviderError):
                            inference = error.inference.to_wire()
                        elif inference is not None and inference.get("error_code") is None:
                            inference["error_code"] = rejection_reason
                        scores = sorted(decision.probabilities.values(), reverse=True) if decision else []
                        write_event(log_path, {"event": "outcome", "outcome": "unknown", "phase": "decide",
                            "step": step, "inference": inference, "reason": rejection_reason,
                            "selected_id": decision.selected_id if decision else None,
                            "selected_probability": decision.probabilities.get(decision.selected_id) if decision else None,
                            "selection_margin": scores[0] - scores[1] if len(scores) >= 2 else None,
                            "observation": observed, "visual": visual_record,
                            "decide_ms": round((time.perf_counter()-decide_started)*1000, 2)})
                        return "unknown"
                elif args.provider == "mock":
                    choice, confidence, probabilities = choose_mock_for_task(
                        task, sources, plan.candidates, history
                    )
                elif args.provider == "s1":
                    try:
                        choice, confidence, probabilities = await asyncio.to_thread(
                            choose_s1_service, request
                        )
                    except S1ServiceError as error:
                        # A tied or malformed score is not an action; fail closed
                        # with a logged outcome instead of a traceback.
                        write_event(log_path, {"event": "outcome", "outcome": "unknown",
                                               "phase": "decide", "step": step,
                                               "candidate_count": len(plan.candidates),
                                               "expected_ids": task.expected_next(history),
                                               "error": "S1ServiceError", "reason": str(error)[:128],
                                               "observation": observed, "visual": visual_record,
                                               "decide_ms": round((time.perf_counter()-decide_started)*1000, 2)})
                        return "unknown"
                else:
                    choice, confidence, probabilities = await asyncio.to_thread(choose_live, request)
                decide_ms = round((time.perf_counter() - decide_started) * 1000, 2)
                # Measurement (#4312): the declared steps due now, and whether the
                # candidate set offered one. IDs only; no values.
                expected_ids = task.expected_next(history)
                offered = {candidate.id.removesuffix(":foreground") for candidate in plan.candidates}
                base_event = {
                    "event": "step",
                    "step": step,
                    "observation": observed,
                    "visual": visual_record,
                    "compose": plan.stats.to_log(),
                    "candidate_count": len(plan.candidates),
                    "schema": request["schema"],
                    "confidence": confidence,
                    "probabilities": probabilities,
                    "decide_ms": decide_ms,
                    "inference": inference,
                    "expected_ids": expected_ids,
                    "expected_offered": bool(set(expected_ids) & offered),
                }
                if choice is None:
                    write_event(log_path, {**base_event, "event": "outcome", "outcome": "abstained"})
                    return "abstained"
                visual = sources.visual.observation if sources.visual is not None else None
                candidate = validate_choice(
                    choice, plan.candidates, current_capture_id=visual.capture_id if visual else None
                )
                assert_in_scope(candidate, args.pid, window_id)
                base_event["candidate"] = candidate.id
                base_event["source"] = candidate.source

                if candidate.id == "reobserve":
                    history.append(task.history_entry(step, candidate.id))
                    write_event(log_path, {**base_event, "tool": None, "act_ms": 0.0})
                    continue
                if candidate.id == "abstain":
                    write_event(log_path, {**base_event, "event": "outcome", "outcome": "abstained"})
                    return "abstained"

                attempt_id = f"{label}:{step}"
                base_event["attempt_id"] = attempt_id
                write_event(log_path, {**base_event, "event": "action_attempt", "attempt_id": attempt_id,
                    "step": step, "tool": candidate.tool, "candidate": candidate.id,
                    "expected_ids": expected_ids,
                    "requested_delivery_mode": candidate.arguments.get("delivery_mode")})
                act_started = time.perf_counter()
                try:
                    assert candidate.tool is not None
                    action_result = await driver.call(candidate.tool, dict(candidate.arguments))
                except (Exception, asyncio.CancelledError) as error:
                    act_ms = round((time.perf_counter() - act_started) * 1000, 2)
                    if is_stale_token_error(error):
                        # Nothing else is dispatched; the next step observes again.
                        history.append(task.history_entry(step, candidate.id, stale=True))
                        write_event(log_path, {**base_event, "tool": candidate.tool,
                                               "act_ms": act_ms, "action_error": "stale_element_token"})
                        continue
                    refusal = background_refusal_code(candidate, error)
                    if refusal is not None:
                        foreground_ids.add(candidate.id)
                        history.append(task.history_entry(step, candidate.id, refusal=refusal))
                        write_event(log_path, {**base_event, "tool": candidate.tool, "act_ms": act_ms,
                                               "action_error": refusal,
                                               "escalation": {"from": "background", "to": "foreground",
                                                              "allowed": task.allow_foreground}})
                        continue
                    write_event(log_path, {**base_event, "event": "outcome", "outcome": "unknown",
                                           "phase": "action", "error": type(error).__name__,
                                           "tool": candidate.tool, "act_ms": act_ms,
                                           "delivery_status": "unresolved",
                                           "cancelled": isinstance(error, asyncio.CancelledError)})
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    return "unknown"
                act_ms = round((time.perf_counter() - act_started) * 1000, 2)
                effect = action_result.get("effect")
                delivery = action_result.get("delivery")
                delivery_mode = delivery.get("mode") if isinstance(delivery, dict) else None
                confirmed = effect == "confirmed" and delivery_mode != "unknown"

                write_event(log_path, {**base_event, "tool": candidate.tool, "act_ms": act_ms,
                                       "requested_delivery_mode": candidate.arguments.get("delivery_mode"),
                                       "delivery_mode": delivery_mode, "delivery": delivery, "effect": effect,
                                       "delivery_status": "confirmed" if confirmed else "uncertain"})
                oracle_started = time.perf_counter()
                outcome = await poll_oracle(task, step)
                oracle_ms = round((time.perf_counter()-oracle_started)*1000, 2)
                write_event(log_path, {"event": "oracle", "step": step, "oracle_ms": oracle_ms, "outcome": outcome})
                reconciled = (delivery_mode != "unknown" and effect == "unverifiable"
                              and task.action_progress(before, task.read_oracle(), candidate.id))
                if confirmed or reconciled:
                    history.append(task.history_entry(step, candidate.id, outcome=plan.outcomes.get(candidate.id)))
                if reconciled:
                    write_event(log_path, {"event": "action_reconciled", "attempt_id": attempt_id,
                        "step": step, "evidence": "independent_oracle"})
                if outcome in {"verified", "refuted"} or not (confirmed or reconciled):
                    write_event(log_path, {"event": "outcome", "outcome": outcome, "step": step})
                    return outcome

            outcome = task.classify(task.read_oracle(), steps=task.max_steps)
            write_event(log_path, {"event": "outcome", "outcome": outcome, "step": task.max_steps})
            return outcome


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=NATIVE_TASK_IDS, required=True)
    parser.add_argument(
        "--provider", choices=("mock", "live", "s1", *NEW_PROVIDERS), default="mock",
        help="s1 reads the loopback decide URL from CUA_S1_DECISION_URL",
    )
    parser.add_argument("--pid", type=int, required=True, help="the running harness process")
    parser.add_argument("--state-file", required=True, help="the harness task-state file path")
    parser.add_argument("--note-text", help="note text for the save-note tasks")
    parser.add_argument("--allow-foreground", action="store_true")
    parser.add_argument("--platform", choices=("macos", "windows", "linux"))
    parser.add_argument("--log", help="optional JSONL output path")
    parser.add_argument("--driver-socket", help="connect only to this Driver daemon socket")
    parser.add_argument("--openai-model", default="gpt-6-luna")
    parser.add_argument("--benchmark-input", action="store_true")
    parser.add_argument("--min-probability", type=float, default=0.6)
    parser.add_argument("--min-margin", type=float, default=0.1)
    parser.add_argument("--max-observation-age-ms", type=int, default=30000)
    args = parser.parse_args(argv)
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in (args.min_probability, args.min_margin)):
        parser.error("probability thresholds must be finite numbers in [0, 1]")
    if args.max_observation_age_ms <= 0:
        parser.error("observation age must be positive")
    return args


def task_from_args(args: argparse.Namespace) -> NativeTask:
    options: dict[str, Any] = {"pid": args.pid, "allow_foreground": args.allow_foreground}
    if args.note_text:
        options["note_text"] = args.note_text
    task = native_task(args.task, Path(args.state_file), **options)
    if getattr(args, "benchmark_input", False):
        parameters = tuple(replace(parameter, secret=False) for parameter in task.parameters)
        goal = task.goal
        if parameters:
            goal += " The deliberately public synthetic note text is " + json.dumps(parameters[0].value) + "."
        task = replace(task, parameters=parameters, goal=goal, cap_order="depth_first")
    return task


def main() -> None:
    args = parse_args()
    if args.provider == "s1":
        s1_service_url()  # fail before touching the app when the service is not configured
    try:
        outcome = asyncio.run(run_task(args, task_from_args(args)))
    except (OracleError, NativeObservationError, ValueError) as error:
        write_event(Path(args.log) if args.log else None,
                    {"event": "outcome", "outcome": "unknown", "error": type(error).__name__})
        raise SystemExit(1) from None
    raise SystemExit(0 if outcome == "verified" else 1)


if __name__ == "__main__":
    main()
