from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import benchmark_web as web
from decision_models import DecisionResult, InferenceEvidence
from decision_providers import ProviderTransport
from standard_llm import choose_standard


def snapshot(url="https://en.wikipedia.org/wiki/Main_Page", title="Wikipedia, the free encyclopedia", value=None):
    return {"target_id": "target", "tab_id": "tab", "snapshot": {"id": "p1", "complete": True},
        "page": {"url": url, "title": title}, "outline": "Public document",
        "refs": [{"ref": "p1:0", "role": "searchbox", "name": "Search Wikipedia", "value": value,
                  "actions": ["type"], "visibility": "in_viewport", "states": {"editable": "plaintext"}},
                 {"ref": "p1:1", "role": "button", "name": "Search", "value": None,
                  "actions": ["click"], "visibility": "in_viewport", "states": {}}]}


class WebsiteBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.task = web.TASKS[0]
        self.snapshot = snapshot()
        self.candidates = web.website_candidates(self.task, self.snapshot)
        self.request = web.decision_request(self.task, self.snapshot, self.candidates, [])
        self.candidate = next(c for c in self.candidates if c.tool == "browser_click")
        scores = {c.id: .9 if c.id == self.candidate.id else .1 / (len(self.candidates)-1) for c in self.candidates}
        self.choice = DecisionResult("selected", "p1", self.candidate.id, "m", .9, scores)

    def dispatch(self, **kwargs):
        options = {"scope": {"target_id": "target", "tab_id": "tab"},
                   "observed_at": time.monotonic(), "require_probabilities": True}
        options.update(kwargs)
        return web.validate_web_dispatch(self.choice, self.request, self.candidates, self.snapshot, **options)

    def test_rotated_forty_five_trial_schedule(self):
        tasks = [t.name for t in web.TASKS]
        rows = web.schedule(tasks, list(web.PROVIDERS), 5)
        self.assertEqual(len(rows), 45)
        self.assertEqual(len({(r["task"], r["provider"], r["repetition"]) for r in rows}), 45)
        self.assertEqual([rows[i*9]["provider"] for i in range(5)],
                         [web.PROVIDERS[i % 3] for i in range(5)])

    def test_ref_scope_age_and_selected_identity(self):
        self.assertIs(self.dispatch(), self.candidate)
        for key in ("target_id", "tab_id"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.dispatch(scope={"target_id": "target", "tab_id": "tab", key: "other"})
        with self.assertRaises(ValueError): self.dispatch(scope={})
        with self.assertRaises(ValueError):
            self.dispatch(observed_at=time.monotonic()-31)
        for altered in (replace(self.choice, capture_id="p0"), replace(self.choice, selected_id="invented")):
            original = self.choice; self.choice = altered
            with self.assertRaises(ValueError): self.dispatch()
            self.choice = original
        for altered in (replace(self.candidate, snapshot_id="p0"),
                        replace(self.candidate, arguments={**self.candidate.arguments, "ref": "p0:1"})):
            original = self.candidates
            self.candidates = tuple(altered if c.id == altered.id else c for c in original)
            with self.assertRaises(ValueError): self.dispatch()
            self.candidates = original
        self.candidates += (self.candidate,)
        with self.assertRaises(ValueError): self.dispatch()

    def test_probability_gate_and_standard_has_no_fabricated_score(self):
        for value in (.59, float("nan"), True):
            scores = dict(self.choice.probabilities); scores[self.candidate.id] = value
            original = self.choice; self.choice = replace(self.choice, probabilities=scores)
            with self.assertRaises(ValueError): self.dispatch()
            self.choice = original
        self.choice = replace(self.choice, confidence=None, probabilities={})
        self.assertIs(self.dispatch(require_probabilities=False), self.candidate)

    def test_candidate_generation_uses_page_controls_not_oracle(self):
        wrong = {"ref": "p1:2", "role": "link", "name": "Unrelated public page", "actions": ["click"],
                 "visibility": "in_viewport", "states": {}}
        self.snapshot["refs"].append(wrong)
        choices = web.website_candidates(self.task, self.snapshot)
        self.assertTrue(any(c.arguments.get("ref") == "p1:2" for c in choices))
        self.assertTrue(all(c.arguments.get("input_route") == "trusted" for c in choices if c.tool == "browser_click"))
        prompt = self.request.provider_observation()
        self.assertNotIn("progress", prompt)
        self.assertNotIn("expected", json.dumps(prompt))

    def test_readiness_ignores_outline_layout_order_but_tracks_controls(self):
        changed = {**self.snapshot, "outline": "Different generic layout node order"}
        self.assertEqual(web.fingerprint(changed), web.fingerprint(self.snapshot))
        changed = snapshot(value="Solar eclipse")
        self.assertNotEqual(web.fingerprint(changed), web.fingerprint(self.snapshot))

    def test_standard_generates_browser_arguments_with_scope_and_route_only_enumerated(self):
        calls = []
        def respond(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200, json={"status": "completed", "model": "m", "output": [{
                "type": "function_call", "name": "browser_click", "arguments": json.dumps(dict(self.candidate.arguments))}]})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result = choose_standard(ProviderTransport(client, "https://provider.invalid/responses", {}, "m"), self.request, self.candidates)
        self.assertEqual(result.selected_id, self.candidate.id)
        self.assertEqual(result.probabilities, {})
        body = calls[0]; inputs = json.loads(body["input"])
        self.assertEqual(inputs["scope"], {"target_id": "target", "tab_id": "tab"})
        self.assertTrue(any(c.get("ref") == "p1:1" for c in inputs["controls"]))
        self.assertNotIn("candidates", inputs)
        for tool in body["tools"]:
            properties = tool["parameters"].get("properties", {})
            for key in ("ref", "text"):
                if key in properties:self.assertNotIn("enum", properties[key])
            if tool["name"] == "browser_click":
                self.assertEqual(properties["input_route"]["enum"], ["trusted"])
                self.assertEqual(properties["delivery_mode"]["enum"], ["background"])

    def test_oracles_reject_similar_wrong_destinations(self):
        wiki = snapshot("https://en.wikipedia.org/wiki/Solar_eclipse", "Solar eclipse - Wikipedia")
        self.assertTrue(web.checkpoint(self.task, wiki, {})["complete"])
        self.assertFalse(web.checkpoint(self.task, snapshot("https://en.wikipedia.org/wiki/Lunar_eclipse", "Lunar eclipse - Wikipedia"), {})["complete"])
        for suffix in ("/w/index.php?title=Main_Page&action=history", "/w/index.php?title=Special:Search&search=Lunar+eclipse", "/wiki/Special:Search"):
            self.assertNotIn("search_results", web.checkpoint(self.task, snapshot("https://en.wikipedia.org"+suffix), {})["marks"])
        search = snapshot("https://en.wikipedia.org/w/index.php?title=Special:Search&search=Solar+eclipse")
        self.assertIn("search_results", web.checkpoint(self.task, search, {})["marks"])
        python = web.TASKS[1]
        good = snapshot("https://docs.python.org/3/library/pathlib.html#pathlib.Path.read_text", "pathlib documentation")
        self.assertTrue(web.checkpoint(python, good, {})["complete"])
        good["page"]["url"] = "https://docs.python.org/3/library/zipfile.html#zipfile.Path.read_text"
        self.assertFalse(web.checkpoint(python, good, {})["complete"])
        hn = web.TASKS[2]
        good = snapshot("https://news.ycombinator.com/item?id=123", "Frozen first story | Hacker News")
        context = {"hn_title": "Frozen first story", "hn_id": "123"}
        self.assertTrue(web.checkpoint(hn, good, context)["complete"])
        for title, item_id in (("Different story", "123"), ("Frozen first story", "999"), ("Frozen first", "123")):
            self.assertFalse(web.checkpoint(hn, good, {"hn_title": title, "hn_id": item_id})["complete"])
        self.assertFalse(web.checkpoint(hn, good, {"hn_title": "Frozen first story"})["complete"])

    def test_action_classification_uses_after_state_not_choice(self):
        before = web.checkpoint(self.task, self.snapshot, {})
        after = web.checkpoint(self.task, snapshot(value="Solar eclipse"), {})
        typing = next(c for c in self.candidates if c.tool == "browser_type")
        self.assertEqual(web.classify_action(self.task, typing, before, after, {"effect": "unverifiable"}), "progress")
        self.assertEqual(web.classify_action(self.task, typing, before, after, {"effect": "refused"}), "dispatch_refused")
        self.assertEqual(web.classify_action(self.task, self.candidate, before, before, {"effect": "unverifiable"}), "noop")
        external = web.checkpoint(self.task, snapshot("https://elsewhere.invalid/", "Other", "Solar eclipse"), {})
        self.assertEqual(external["marks"], [])
        self.assertEqual(web.classify_action(self.task, self.candidate, before, external, {"effect": "unverifiable"}), "wrong")
        unrelated = snapshot(value="Solar eclipse"); unrelated["refs"][0]["name"] = "Email"
        self.assertNotIn("query_entered", web.checkpoint(self.task, unrelated, {})["marks"])

    def test_failures_and_unknown_actions_stay_in_denominators(self):
        def trial(status, classifications, elapsed):
            return {"provider": "p", "task": "t", "status": status, "actions": [
                {"classification": c, "cycle_ms": 3} for c in classifications], "task_ms": elapsed,
                "wall_ms": elapsed+1, "inferences": [{"elapsed_ms": 2}]}
        group = web.aggregate([trial("success", ["progress"], 10),
            trial("refusal", ["dispatch_refused", "unknown", "wrong"], 3)])[0]
        self.assertEqual((group["successes"], group["runs"]), (1, 2))
        self.assertEqual(group["action_attempts"], 4)
        self.assertEqual(group["verified_progress_rate"], .25)
        self.assertEqual(group["progress_upper_bound"], .5)
        self.assertEqual(group["successful_task"]["median_ms"], 10)
        self.assertEqual(group["all_trial_wall"]["n"], 2)
        self.assertEqual(group["verified_action_cycle"], {"n": 1, "median_ms": 3, "min_ms": 3, "max_ms": 3})


class WebsiteTrialTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_finishes_despite_layout_outline_jitter(self):
        first = snapshot(); second = {**first, "outline": "Reordered generic layout"}
        driver = SimpleNamespace(call=mock.AsyncMock(side_effect=[first, second]))
        trial = {"phase_ms": {"observe": 0, "wait": 0}, "observation_count": 0, "pending_phase": None}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(web.asyncio, "sleep", new=mock.AsyncMock()):
            result, observed_at = await web.observe(driver, {}, Path(directory), trial, time.monotonic()+1)
        self.assertIs(result, second)
        self.assertEqual(driver.call.await_count, 2)
        self.assertLessEqual(observed_at, time.monotonic())

    async def trial(self, chooser, *, refusal=False, timeout=1):
        self.calls = []
        result_snapshot = snapshot("https://en.wikipedia.org/wiki/Solar_eclipse", "Solar eclipse - Wikipedia")
        driver_calls = self.calls

        class Session:
            async def initialize(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def call_tool(self, name, arguments):
                driver_calls.append(name)
                data = {}
                if name == "browser_prepare": data = {"prepared_pid": 987654}
                if name == "get_browser_state":
                    data = {"binding_quality": "exact", "mutation_allowed": True, "target_id": "target", "tabs": [{"tab_id": "tab"}]}
                if name == "browser_click":
                    data = ({"status": "refused", "refusal": {"code": "browser_ref_stale"}} if refusal
                            else {"effect": "unverifiable"})
                return SimpleNamespace(isError=False, structuredContent=data)

        @asynccontextmanager
        async def stdio(*args, **kwargs): yield None, None

        observations = [snapshot(), snapshot(), result_snapshot]
        async def observe(*args): return observations.pop(0), time.monotonic()

        options = argparse.Namespace(driver_socket="unused", openai_model="m", timeout=timeout, max_decisions=2)
        spec = {"order": 1, "provider": "standard-llm", "task": "wikipedia", "repetition": 1}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(web, "stdio_client", stdio), \
                mock.patch.object(web, "ClientSession", return_value=Session()), \
                mock.patch.object(web, "provider_transport", return_value=SimpleNamespace(model="m")), \
                mock.patch.object(web, "wait_for_window", new=mock.AsyncMock(return_value={"window_id": "w", "bounds": {}})), \
                mock.patch.object(web, "observe", observe), mock.patch.object(web, "choose_standard", side_effect=chooser), \
                mock.patch.object(web.os, "kill", side_effect=ProcessLookupError):
            result = await web.run_trial(spec, web.TASKS[0], options, Path(directory))
            saved = json.loads(next(Path(directory).glob("*/result.json")).read_text())
            self.assertEqual(saved, result)
            self.assertTrue(result["session_ended"])
            self.assertTrue(result["owned_browser_exited"])
            self.assertEqual(self.calls.count("end_session"), 1)
            return result

    @staticmethod
    def choice(transport, request, candidates):
        candidate = next(c for c in candidates if c.tool == "browser_click")
        return DecisionResult("selected", request.capture_id, candidate.id, "m", None, {},
                              inference=InferenceEvidence("m", elapsed_ms=1, request_count=1))

    async def test_loop_success_uses_post_state_and_ends_owned_session(self):
        result = await self.trial(self.choice)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["actions"][0]["classification"], "progress")
        self.assertGreater(result["actions"][0]["cycle_ms"], 0)
        self.assertEqual(result["inferences"][0]["request_count"], 1)

    async def test_real_driver_refusal_is_not_unknown_progress(self):
        result = await self.trial(self.choice, refusal=True)
        self.assertEqual(result["status"], "dispatch_refused")
        self.assertEqual(result["actions"][0]["error_code"], "browser_ref_stale")
        group = web.aggregate([result])[0]
        self.assertEqual(group["non_refused_attempts"], 0)
        self.assertEqual(group["progress_upper_bound"], 0)
        self.assertIsNotNone(result["phase_ms"]["dispatch"])

    async def test_slow_inference_cannot_dispatch_after_deadline(self):
        def slow(*args):
            time.sleep(.08)
            return self.choice(*args)
        result = await self.trial(slow, timeout=.02)
        self.assertEqual(result["status"], "timeout")
        self.assertNotIn("browser_click", self.calls)
        self.assertEqual(result["inferences"][0]["state"], "pending")
        self.assertIsNone(result["inferences"][0]["request_count"])
        self.assertIsNone(result["phase_ms"]["decide"])

    async def test_interrupted_request_remains_in_evidence(self):
        result = await self.trial(asyncio.CancelledError())
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["inferences"][0]["state"], "pending")
        self.assertEqual(result["pending_phase"], "decide")
        self.assertNotIn("browser_click", self.calls)

    async def test_hn_identity_is_independent_and_rejects_ambiguous_titles(self):
        html = '<tr class="athing submission" id="42"><td><span class="titleline"><a href="https://example.org/">A &amp; B</a></span></td></tr>'
        async def freeze(content, context):
            with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, text=content))) as client:
                await web.freeze_hn_identity(client, context, Path(directory), time.monotonic()+1)
        context = {"hn_title": "A & B"}
        await freeze(html, context)
        self.assertEqual(context["hn_id"], "42")
        for content in (html+html.replace('id="42"', 'id="43"'), html.replace('A &amp; B', 'Other')):
            with self.assertRaisesRegex(RuntimeError, "oracle_identity_unknown"):
                await freeze(content, {"hn_title": "A & B"})


if __name__ == "__main__":
    unittest.main()
