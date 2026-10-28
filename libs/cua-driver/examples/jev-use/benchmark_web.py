"""Compare real public websites through Cua Driver; requires explicit live-use opt-in."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import signal
import statistics
import sys
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[3]
sys.path.insert(0, str(BASE / "python"))
from decision_models import DecisionRequest, DecisionResult, choose
from decision_providers import MicrosoftDecisionModel, OpenAIDecisionModel, ProviderError, provider_transport
from driver_env import driver_environment
from run import Driver, DriverToolError, select_tab_id, wait_for_window
from sources import Candidate
from standard_llm import choose_standard

PROVIDERS = ("standard-llm", "openai-decisions", "microsoft-decision1")
MAX_CANDIDATES = 96
MAX_AGE_MS = 30000
OBSERVATION_TIMEOUT = 60
MIN_PROBABILITY = .6
MIN_MARGIN = .1
BLOCKED_LABELS = {"login", "log in", "sign in", "sign up", "submit", "hide", "upvote", "downvote", "edit",
                  "donate", "create account", "download these documents", "ログイン", "登録", "編集", "寄付"}
ROLES = {"link", "button", "checkbox", "combobox", "searchbox", "textbox", "tab", "menuitem", "option"}
VISIBLE = {"in_viewport", "near_viewport"}


@dataclass(frozen=True)
class WebsiteTask:
    name: str
    start_url: str
    goal: str
    query: str | None = None


TASKS = (
    WebsiteTask("wikipedia", "https://en.wikipedia.org/", "Search Wikipedia for Solar eclipse and open the Solar eclipse encyclopedia article.", "Solar eclipse"),
    WebsiteTask("python-docs", "https://docs.python.org/3/", "Search this Python documentation for Path.read_text and open the pathlib.Path.read_text method documentation.", "Path.read_text"),
    WebsiteTask("hacker-news", "https://news.ycombinator.com/", "Open the new submissions feed, then open the discussion of the first story in that feed. Open its Hacker News discussion, not the external story website."),
)


def website_candidates(task: WebsiteTask, snapshot: dict) -> tuple[Candidate, ...]:
    scope = {key: snapshot[key] for key in ("target_id", "tab_id")}
    candidates = []
    seen = set()
    for ref in snapshot.get("refs", []):
        name = str(ref.get("name") or "").strip()
        if (not name or name.casefold() in BLOCKED_LABELS or ref.get("role") not in ROLES
                or ref.get("visibility") not in VISIBLE or ref.get("states", {}).get("disabled") is True
                or ref["ref"] in seen):
            continue
        seen.add(ref["ref"])
        actions = ref.get("actions") or []
        common = dict(source="page", snapshot_id=snapshot["snapshot"]["id"])
        if "click" in actions:
            candidates.append(Candidate("page:click:" + ref["ref"], f'Click {ref["role"]} {name[:180]!r}',
                "browser_click", {**scope, "ref": ref["ref"], "input_route": "trusted", "delivery_mode": "background"}, **common))
        if "type" in actions and task.query is not None:
            candidates.append(Candidate("page:type:" + ref["ref"], f'Type {task.query!r} into {ref["role"]} {name[:180]!r}',
                "browser_type", {**scope, "ref": ref["ref"], "text": task.query, "replace": True}, **common))
    return tuple(candidates[:MAX_CANDIDATES]) + (
        Candidate("reobserve", "Wait and read the page again without input.", None, {}),
        Candidate("abstain", "Stop without further input.", None, {}))


def decision_request(task: WebsiteTask, snapshot: dict, candidates: tuple[Candidate, ...], history: list[dict]) -> DecisionRequest:
    observation = {"page": snapshot["page"], "outline": snapshot.get("outline", "")[:16000],
        "controls": [{key: ref.get(key) for key in ("role", "name", "value", "states", "visibility")}
                     for ref in snapshot.get("refs", []) if ref.get("visibility") in VISIBLE][:100]}
    return DecisionRequest(task.goal, snapshot["snapshot"]["id"], (), tuple(history[-16:]),
        tuple({"id": c.id, "description": c.description} for c in candidates),
        snapshot_id=snapshot["snapshot"]["id"], observation_text=json.dumps(observation, ensure_ascii=False))


def validate_web_dispatch(result: DecisionResult, request: DecisionRequest, candidates: tuple[Candidate, ...],
                          snapshot: dict, *, scope: dict, observed_at: float, require_probabilities: bool) -> Candidate:
    sid = snapshot["snapshot"]["id"]
    if result.kind == "error" or result.selected_id is None:
        raise ValueError("decision_error")
    if result.capture_id != sid or request.capture_id != sid or request.snapshot_id != sid:
        raise ValueError("snapshot_binding")
    age = (time.monotonic() - observed_at) * 1000
    if not math.isfinite(age) or not 0 <= age <= MAX_AGE_MS:
        raise ValueError("observation_expired")
    matches = [c for c in candidates if c.id == result.selected_id]
    if len(matches) != 1:
        raise ValueError("unknown_candidate")
    candidate = matches[0]
    if candidate.tool is None:
        return candidate
    if (set(scope) != {"target_id", "tab_id"} or not all(scope.values())
            or any(snapshot.get(k) != v or candidate.arguments.get(k) != v for k, v in scope.items())):
        raise ValueError("candidate_scope")
    refs = {ref["ref"]: ref for ref in snapshot.get("refs", [])}
    ref = refs.get(candidate.arguments.get("ref"))
    action = {"browser_click": "click", "browser_type": "type"}.get(candidate.tool)
    if (candidate.source != "page" or candidate.snapshot_id != sid or ref is None
            or action is None or action not in (ref.get("actions") or [])):
        raise ValueError("ref_binding")
    if require_probabilities:
        scores = result.probabilities
        if (set(scores) != {c.id for c in candidates} or any(type(p) not in {int, float}
                or not math.isfinite(p) or not 0 <= p <= 1 for p in scores.values())
                or abs(sum(scores.values()) - 1) > .02):
            raise ValueError("invalid_probabilities")
        ordered = sorted(scores.values(), reverse=True)
        if (len(ordered) < 2 or scores[candidate.id] != ordered[0] or ordered[0] < MIN_PROBABILITY
                or ordered[0] - ordered[1] < MIN_MARGIN):
            raise ValueError("probability_gate")
    return candidate


def fingerprint(snapshot: dict) -> str:
    content = {"page": snapshot.get("page"),
               "controls": [{k: r.get(k) for k in ("role", "name", "value", "states", "visibility")}
                            for r in snapshot.get("refs", [])]}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def page_problem(snapshot: dict) -> str | None:
    title = str(snapshot.get("page", {}).get("title", "")).casefold()
    if any(word in title for word in ("just a moment", "access denied", "attention required", "captcha")):
        return "site_blocked"
    if any(word in title for word in ("site can’t be reached", "site cannot be reached", "サイトにアクセスできません")):
        return "site_error"
    return None


def ready(snapshot: dict) -> bool:
    page = snapshot.get("page", {})
    title, url = str(page.get("title", "")), str(page.get("url", ""))
    return bool(title and title not in {url, url.removeprefix("https://"), urlsplit(url).hostname}
                and any(r.get("visibility") in VISIBLE for r in snapshot.get("refs", [])))


class HNStories(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stories = []
        self.item_id = None
        self.titleline = False
        self.parts = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "tr" and "athing" in (attrs.get("class") or "").split():
            self.item_id = attrs.get("id")
        if tag == "span" and attrs.get("class") == "titleline":
            self.titleline = True
        if tag == "a" and self.titleline:
            self.parts = []

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.parts is not None:
            self.stories.append((" ".join("".join(self.parts).split()), self.item_id))
            self.parts = None
            self.titleline = False


def update_context(task: WebsiteTask, snapshot: dict, context: dict) -> None:
    page = snapshot.get("page", {}); parsed = urlsplit(page.get("url", ""))
    if (task.name == "hacker-news" and parsed.hostname == "news.ycombinator.com"
            and parsed.path == "/newest" and not context.get("hn_title")):
        navigation = {"hacker news", "new", "past", "comments", "ask", "show", "jobs", "submit", "login", "y combinator"}
        links = [r for r in snapshot.get("refs", []) if r.get("role") == "link" and r.get("visibility") in VISIBLE
                 and r.get("name") and r["name"].casefold() not in navigation]
        if links:
            context["hn_title"] = links[0]["name"]


async def freeze_hn_identity(client: httpx.Client, context: dict, output: Path, deadline: float) -> None:
    if not context.get("hn_title") or context.get("hn_id"):
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("trial_deadline")
    response = await asyncio.wait_for(asyncio.to_thread(client.get, "https://news.ycombinator.com/newest",
        timeout=min(10, remaining)), remaining)
    response.raise_for_status()
    (output / "oracle-newest.html").write_bytes(response.content)
    parser = HNStories(); parser.feed(response.text)
    matches = [item_id for title, item_id in parser.stories if title == context["hn_title"]]
    if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0].isdigit():
        raise RuntimeError("oracle_identity_unknown")
    context["hn_id"] = matches[0]


def checkpoint(task: WebsiteTask, snapshot: dict, context: dict) -> dict:
    page = snapshot.get("page", {}); url = page.get("url", ""); parsed = urlsplit(url)
    title = str(page.get("title", "")); marks = set()
    on_origin = parsed.hostname == urlsplit(task.start_url).hostname
    fields = [r for r in snapshot.get("refs", []) if "type" in (r.get("actions") or [])
              and r.get("role") in {"textbox", "searchbox", "combobox"}
              and ("search" in str(r.get("name", "")).casefold())] if on_origin else []
    if task.query and any(r.get("value") == task.query for r in fields):
        marks.add("query_entered")
    if task.name == "wikipedia":
        query = parse_qs(parsed.query)
        search_page = (parsed.path == "/wiki/Special:Search" or
                       parsed.path == "/w/index.php" and query.get("title") == ["Special:Search"])
        if on_origin and search_page and query.get("search") == [task.query]:
            marks.add("search_results")
        complete = parsed.hostname == "en.wikipedia.org" and unquote(parsed.path) == "/wiki/Solar_eclipse" and title.startswith("Solar eclipse")
    elif task.name == "python-docs":
        if on_origin and parsed.path.endswith("/search.html") and parse_qs(parsed.query).get("q") == [task.query]:
            marks.add("search_results")
        complete = (parsed.hostname == "docs.python.org" and parsed.path.endswith("/library/pathlib.html")
                    and parsed.fragment == "pathlib.Path.read_text" and "pathlib" in title)
    elif task.name == "hacker-news":
        if on_origin and parsed.path == "/newest" and context.get("hn_id"):
            marks.add("new_feed")
        complete = bool(parsed.hostname == "news.ycombinator.com" and parsed.path == "/item"
                        and context.get("hn_id") and parse_qs(parsed.query).get("id") == [context["hn_id"]]
                        and title == context["hn_title"] + " | Hacker News")
    else:
        raise ValueError("unknown_task")
    if complete:
        marks.add("complete")
    return {"url": url, "title": title, "marks": sorted(marks), "complete": complete,
            "fields": [{k: r.get(k) for k in ("name", "value", "states")} for r in fields],
            "fingerprint": fingerprint(snapshot)}


def classify_action(task: WebsiteTask, candidate: Candidate, before: dict, after: dict, result: dict) -> str:
    if result.get("effect") == "refused":
        return "dispatch_refused"
    if result.get("effect") in {"partial", "suspected_noop"}:
        return "unknown"
    if result.get("error"):
        return "dispatch_error"
    if urlsplit(after["url"]).hostname != urlsplit(task.start_url).hostname:
        return "wrong"
    if set(after["marks"]) - set(before["marks"]):
        return "progress"
    if candidate.tool == "browser_type":
        if any(f.get("value") == task.query for f in after["fields"]):
            return "noop" if "query_entered" in before["marks"] else "preparation"
        return "unknown"
    if after["fingerprint"] == before["fingerprint"]:
        return "noop"
    if any(f.get("states", {}).get("focused") is True for f in after["fields"]) and not any(
            f.get("states", {}).get("focused") is True for f in before["fields"]):
        return "preparation"
    return "unknown"


def schedule(tasks: list[str], providers: list[str], repetitions: int) -> list[dict]:
    rows = []
    for repetition in range(repetitions):
        rotated = providers[repetition % len(providers):] + providers[:repetition % len(providers)]
        for task in tasks:
            for provider in rotated:
                rows.append({"order": len(rows) + 1, "task": task, "provider": provider, "repetition": repetition + 1})
    return rows


def distribution(values: list[float]) -> dict:
    return {"n": len(values), "median_ms": statistics.median(values) if values else None,
            "min_ms": min(values) if values else None, "max_ms": max(values) if values else None}


def aggregate(trials: list[dict]) -> list[dict]:
    groups = []
    for provider, task in dict.fromkeys((t["provider"], t["task"]) for t in trials):
        runs = [t for t in trials if (t["provider"], t["task"]) == (provider, task)]
        successful = [t for t in runs if t["status"] == "success"]
        classes = Counter(a["classification"] for t in runs for a in t["actions"])
        attempted = sum(classes.values()); executed = attempted - classes["dispatch_refused"]
        useful = classes["progress"] + classes["preparation"]
        groups.append({"provider": provider, "task": task, "runs": len(runs), "successes": len(successful),
            "statuses": dict(Counter(t["status"] for t in runs)), "action_attempts": attempted,
            "non_refused_attempts": executed, "classifications": dict(classes),
            "verified_progress_rate": useful / attempted if attempted else None,
            "progress_upper_bound": (useful + classes["unknown"] + classes["dispatch_error"]) / attempted if attempted else None,
            "verified_action_cycle": distribution([a["cycle_ms"] for t in runs for a in t["actions"]
                if a["classification"] in {"progress", "preparation"}]),
            "successful_task": distribution([t["task_ms"] for t in successful]),
            "successful_inference": distribution([sum(i["elapsed_ms"] for i in t["inferences"]) for t in successful]),
            "all_trial_wall": distribution([t["wall_ms"] for t in runs])})
    return groups


def emit(path: Path, row: dict) -> None:
    with path.open("a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


async def observe(driver: Driver, scope: dict, output: Path, trial: dict, deadline: float) -> tuple[dict, float]:
    until = min(deadline, time.monotonic() + OBSERVATION_TIMEOUT)
    previous = None
    while time.monotonic() < until:
        trial["pending_phase"] = "observe"
        started = time.monotonic()
        snapshot = await asyncio.wait_for(driver.call("get_browser_state", {**scope, "snapshot_format": "semantic_v2"}), until - time.monotonic())
        trial["phase_ms"]["observe"] += (time.monotonic() - started) * 1000
        trial["pending_phase"] = None
        trial["observation_count"] += 1
        name = f'observation-{trial["observation_count"]:03d}.json'
        (output / name).write_text(json.dumps(snapshot, ensure_ascii=False))
        if problem := page_problem(snapshot):
            raise RuntimeError(problem)
        current = fingerprint(snapshot)
        if ready(snapshot) and current == previous:
            return snapshot, time.monotonic()
        previous = current if ready(snapshot) else None
        trial["pending_phase"] = "wait"
        started = time.monotonic()
        await asyncio.sleep(min(.5, max(0, until - time.monotonic())))
        trial["phase_ms"]["wait"] += (time.monotonic() - started) * 1000
        trial["pending_phase"] = None
    raise TimeoutError("observation_not_stable")


async def run_trial(spec: dict, task: WebsiteTask, args: argparse.Namespace, output: Path) -> dict:
    trial = {**spec, "status": "setup_failed", "actions": [], "inferences": [], "decisions": [],
             "phase_ms": {k: 0.0 for k in ("observe", "wait", "candidates", "decide", "dispatch", "oracle")},
             "observation_count": 0, "pending_phase": None, "task_ms": None, "setup_ms": None,
             "host_load_start": list(os.getloadavg())}
    path = output / f'{spec["order"]:03d}-{task.name}-{spec["provider"]}'
    path.mkdir(); log = path / "events.jsonl"
    started = time.monotonic(); task_started = None; driver = None; prepared_pid = None
    history = []; context = {}
    params = StdioServerParameters(command=os.environ.get("CUA_DRIVER_BIN", "cua-driver"),
        args=["mcp", "--socket", args.driver_socket], env=driver_environment())
    try:
        with (path / "transport.log").open("w") as errors, httpx.Client(timeout=60, follow_redirects=False,
                transport=httpx.HTTPTransport(retries=0)) as client:
            transport = provider_transport(spec["provider"], client, args.openai_model)
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize(); driver = Driver(session, "website-" + uuid.uuid4().hex[:12])
                    try:
                        prepared = await asyncio.wait_for(driver.call("browser_prepare", {"allow_launch": True, "profile": {"mode": "isolated_new"}}), 60)
                        prepared_pid = int(prepared["prepared_pid"]); trial["owned_browser_pid"] = prepared_pid
                        window = await wait_for_window(driver, prepared_pid); trial["window_bounds"] = window["bounds"]
                        bound = await driver.call("get_browser_state", {"pid": prepared_pid, "window_id": window["window_id"]})
                        (path / "binding.json").write_text(json.dumps(bound))
                        if bound.get("binding_quality") != "exact" or bound.get("mutation_allowed") is not True:
                            raise RuntimeError("binding_unproven")
                        scope = {"target_id": bound["target_id"], "tab_id": select_tab_id(bound["tabs"])}
                        await driver.call("browser_navigate", {**scope, "url": task.start_url})
                        await observe(driver, scope, path, trial, time.monotonic() + 45)
                        trial["setup_ms"] = (time.monotonic() - started) * 1000
                        trial["phase_ms"] = {k: 0.0 for k in trial["phase_ms"]}
                        task_started = time.monotonic(); deadline = task_started + args.timeout
                        snapshot, observed_at = await observe(driver, scope, path, trial, deadline)
                        trial["initial"] = checkpoint(task, snapshot, context)
                        if (urlsplit(trial["initial"]["url"]).hostname != urlsplit(task.start_url).hostname
                                or trial["initial"]["complete"] or "query_entered" in trial["initial"]["marks"]):
                            raise RuntimeError("initial_state_invalid")
                        trial["status"] = "budget_exhausted"
                        for step in range(1, args.max_decisions + 1):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("trial_deadline")
                            update_context(task, snapshot, context)
                            phase = time.monotonic()
                            await freeze_hn_identity(client, context, path, deadline)
                            trial["phase_ms"]["oracle"] += (time.monotonic() - phase) * 1000
                            before = checkpoint(task, snapshot, context)
                            if before["complete"]:
                                trial["status"] = "success"; break
                            decision_started = phase = time.monotonic()
                            candidates = website_candidates(task, snapshot)
                            request = decision_request(task, snapshot, candidates, history)
                            trial["phase_ms"]["candidates"] += (time.monotonic() - phase) * 1000
                            emit(log, {"event": "decision_start", "step": step, "snapshot": snapshot["snapshot"],
                                       "page": snapshot["page"], "candidates": [c.id for c in candidates]})
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("trial_deadline")
                            client.timeout = httpx.Timeout(min(60, remaining))
                            trial["pending_phase"] = "decide"; phase = time.monotonic()
                            inference = {"step": step, "state": "pending", "requested_model": transport.model,
                                "elapsed_ms": None, "request_count": None, "input_tokens": None, "output_tokens": None}
                            trial["inferences"].append(inference)
                            try:
                                if spec["provider"] == "standard-llm":
                                    choice = await asyncio.wait_for(asyncio.to_thread(choose_standard, transport, request, candidates), remaining)
                                else:
                                    model = OpenAIDecisionModel(transport) if spec["provider"] == "openai-decisions" else MicrosoftDecisionModel(transport)
                                    choice = await asyncio.wait_for(asyncio.to_thread(choose, model, request), remaining)
                            except ProviderError as error:
                                if error.inference:
                                    inference.update(error.inference.to_wire())
                                inference["state"] = "failed"
                                trial["phase_ms"]["decide"] += (time.monotonic() - phase) * 1000
                                trial["pending_phase"] = None
                                trial["status"] = error.code
                                break
                            finally:
                                inference["wait_elapsed_ms"] = (time.monotonic() - phase) * 1000
                            trial["phase_ms"]["decide"] += (time.monotonic() - phase) * 1000
                            trial["pending_phase"] = None
                            inference["state"] = "completed"
                            if choice.inference:
                                inference.update(choice.inference.to_wire())
                            if time.monotonic() >= deadline:
                                raise TimeoutError("trial_deadline")
                            decision = {"step": step, "kind": choice.kind, "selected_id": choice.selected_id,
                                "selected_probability": choice.probabilities.get(choice.selected_id), "reason": choice.reason}
                            trial["decisions"].append(decision); emit(log, {"event": "decision", **decision})
                            if choice.kind == "error":
                                trial["status"] = (choice.inference.error_code if choice.inference else None) or "invalid_decision"; break
                            try:
                                candidate = validate_web_dispatch(choice, request, candidates, snapshot, scope=scope,
                                    observed_at=observed_at, require_probabilities=spec["provider"] != "standard-llm")
                            except ValueError as error:
                                trial["status"] = str(error); break
                            if candidate.id == "abstain":
                                trial["status"] = "abstained"; break
                            if candidate.id == "reobserve":
                                snapshot, observed_at = await observe(driver, scope, path, trial, deadline); continue
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("trial_deadline")
                            action = {"step": step, "candidate": candidate.id, "description": candidate.description,
                                      "arguments": dict(candidate.arguments), "classification": "unknown", "before": before}
                            trial["actions"].append(action); emit(log, {"event": "action_attempt", **action})
                            trial["pending_phase"] = "dispatch"; phase = time.monotonic()
                            try:
                                result = await asyncio.wait_for(driver.call(candidate.tool, dict(candidate.arguments)), remaining)
                            except DriverToolError as error:
                                action["classification"] = "dispatch_refused" if error.refused else "dispatch_error"
                                action["error_code"] = error.code
                                trial["status"] = action["classification"]
                                action["dispatch_ms"] = (time.monotonic() - phase) * 1000
                                action["cycle_ms"] = (time.monotonic() - decision_started) * 1000
                                trial["phase_ms"]["dispatch"] += action["dispatch_ms"]
                                trial["pending_phase"] = None
                                emit(log, {"event": "action_result", **action})
                                break
                            action["dispatch_ms"] = (time.monotonic() - phase) * 1000
                            trial["phase_ms"]["dispatch"] += action["dispatch_ms"]; trial["pending_phase"] = None
                            action["result"] = result
                            snapshot, observed_at = await observe(driver, scope, path, trial, deadline)
                            phase = time.monotonic(); update_context(task, snapshot, context)
                            await freeze_hn_identity(client, context, path, deadline)
                            after = checkpoint(task, snapshot, context); action["after"] = after
                            action["classification"] = classify_action(task, candidate, before, after, result)
                            action["cycle_ms"] = (time.monotonic() - decision_started) * 1000
                            trial["phase_ms"]["oracle"] += (time.monotonic() - phase) * 1000
                            emit(log, {"event": "action_result", **action})
                            history.append({"selected_id": candidate.description, "outcome": "operation attempted; inspect the current page"})
                            if after["complete"]:
                                trial["status"] = "success"; break
                            if result.get("effect") in {"partial", "suspected_noop"}:
                                trial["status"] = "unresolved_delivery"; break
                            if action["classification"] in {"noop", "unknown"} and before["url"] == after["url"]:
                                trial["status"] = "unresolved_effect"; break
                        trial["final"] = checkpoint(task, snapshot, context)
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        trial["status"] = "interrupted"
                    except Exception as error:
                        trial["status"] = "timeout" if isinstance(error, (TimeoutError, asyncio.TimeoutError)) else "driver_error"
                        trial["error"] = error.code if isinstance(error, DriverToolError) else type(error).__name__
                    finally:
                        if task_started is not None:
                            trial["task_ms"] = (time.monotonic() - task_started) * 1000
                        if trial["pending_phase"]:
                            trial["phase_ms"][trial["pending_phase"]] = None
                        trial["oracle_context"] = context
                        try:
                            await asyncio.wait_for(asyncio.shield(driver.call("end_session", {})), 15)
                            trial["session_ended"] = True
                            trial["owned_browser_exited"] = prepared_pid is None
                            for _ in range(50):
                                if prepared_pid is None:
                                    break
                                try:
                                    os.kill(prepared_pid, 0)
                                except ProcessLookupError:
                                    trial["owned_browser_exited"] = True
                                    break
                                await asyncio.sleep(.1)
                            else:
                                trial["owned_browser_exited"] = False
                        except BaseException:
                            trial["session_ended"] = False
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
            trial["status"] = "interrupted"
        elif trial["status"] != "interrupted":
            trial["status"] = "transport_error"; trial["error"] = type(error).__name__
    finally:
        trial["wall_ms"] = (time.monotonic() - started) * 1000
        trial["host_load_end"] = list(os.getloadavg())
        (path / "result.json").write_text(json.dumps(trial, indent=2, ensure_ascii=False) + "\n")
    return trial


def provenance(args: argparse.Namespace) -> dict:
    paths = sorted(set((BASE / "python").glob("*.py")) | {Path(__file__), BASE / "pyproject.toml", BASE / "uv.lock"})
    return {"started_utc": datetime.now(timezone.utc).isoformat(), "tasks": [asdict(t) for t in TASKS],
        "arguments": vars(args), "candidate_cap": MAX_CANDIDATES, "min_probability": MIN_PROBABILITY,
        "min_margin": MIN_MARGIN, "max_age_ms": MAX_AGE_MS, "input_route": "trusted", "delivery_mode": "background",
        "observation_timeout_s": OBSERVATION_TIMEOUT,
        "profile": "isolated_new per trial", "window_size": "Driver launch900x640", "load_average": os.getloadavg(),
        "cpu_count": os.cpu_count(), "source_sha256": {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "platform": platform.platform(), "python": platform.python_version(),
        "packages": {name: version(name) for name in ("httpx", "mcp")},
        "driver_sha256": hashlib.sha256(Path(os.environ["CUA_DRIVER_BIN"]).read_bytes()).hexdigest()}


async def main(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, asyncio.current_task().cancel)
    output = Path(args.output_dir).resolve(); output.mkdir(parents=True, mode=0o700)
    if any(output.iterdir()):
        raise ValueError("output directory must be empty")
    data = {"complete": False, "metadata": provenance(args), "trials": [], "groups": []}
    lookup = {t.name: t for t in TASKS}
    try:
        for spec in schedule(args.task or list(lookup), args.providers, args.repetitions):
            result = await run_trial(spec, lookup[spec["task"]], args, output)
            data["trials"].append(result); data["groups"] = aggregate(data["trials"])
            (output / "summary.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            print(json.dumps({k: result[k] for k in ("order", "task", "provider", "status")}), flush=True)
            if (result["status"] == "interrupted" or result.get("session_ended") is not True
                    or result.get("owned_browser_exited") is not True
                    or any(i["state"] == "pending" for i in result["inferences"])):
                return 130
        data["complete"] = True
    finally:
        (output / "summary.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return 0 if all(t["status"] == "success" for t in data["trials"]) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live-sites", action="store_true", required=True)
    parser.add_argument("--driver-socket", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--providers", nargs="+", choices=PROVIDERS, default=list(PROVIDERS))
    parser.add_argument("--task", action="append", choices=[t.name for t in TASKS])
    parser.add_argument("--openai-model", default="gpt-6-luna")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-decisions", type=int, default=8)
    options = parser.parse_args()
    if options.repetitions < 1 or options.timeout <= 0 or options.max_decisions < 1 or len(set(options.providers)) != len(options.providers):
        parser.error("positive budgets and unique providers are required")
    raise SystemExit(asyncio.run(main(options)))
