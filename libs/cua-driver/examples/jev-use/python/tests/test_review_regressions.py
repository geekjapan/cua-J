from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_native as runner
from decision_models import DecisionResult, InferenceEvidence, choose
from decision_providers import MicrosoftDecisionModel, OpenAIDecisionModel, ProviderError
from native import NativeObservation
from sources import NativeAccessibilitySource
from standard_llm import choose_standard
from tasks import TaskSources
from test_decision_providers import request, transport


class RunnerRegressionTests(unittest.TestCase):
    def run_fake(self, *, mode="unknown", progress=False, error=None, provider="mock", initial=0, effect="unverifiable"):
        fixture = Path(__file__).resolve().parents[2] / "fixtures/native/appkit-window-state-initial-v1.json"
        payload = json.loads(fixture.read_text())
        args = runner.parse_args(["--task", "appkit-counter", "--pid", str(payload["pid"]), "--state-file", "/unused", "--provider", provider])
        observation = NativeObservation.from_window_state(payload, expected_pid=payload["pid"], expected_window_id=payload["window_id"])
        sources = TaskSources(ax=NativeAccessibilitySource.from_observation(observation, "macos"))
        state = {"counter": initial, "distractor_actions": 0}
        task = replace(runner.task_from_args(args), oracle=SimpleNamespace(read=lambda: dict(state)))
        actions = []
        events = []
        class Driver:
            def __init__(self, *args): pass
            async def call(self, name, arguments):
                if name == "list_windows":
                    return {"windows": [{"window_id": payload["window_id"], "title": task.scope.window_title}]}
                actions.append(name)
                if error:
                    raise error
                if progress:
                    state["counter"] += 1
                return {"effect": effect, "delivery": {"mode": mode}}
        @asynccontextmanager
        async def stdio(*args): yield (None, None)
        @asynccontextmanager
        async def session(*args):
            yield SimpleNamespace(initialize=mock.AsyncMock(), list_tools=mock.AsyncMock(return_value=SimpleNamespace(tools=[])))
        async def oracle(task, step): return task.classify(task.read_oracle(), steps=step)
        with mock.patch.object(runner, "stdio_client", stdio), mock.patch.object(runner, "ClientSession", session), \
                mock.patch.object(runner, "Driver", Driver), mock.patch.object(runner, "write_event", side_effect=lambda path, event: events.append(event)), \
                mock.patch.object(runner, "observe_step", mock.AsyncMock(return_value=(sources, {"observe_ms": 123, "observed_at": 0}))), \
                mock.patch.object(runner, "maybe_visual", mock.AsyncMock(side_effect=lambda driver, task, sources, *args: (sources, {}))), \
                mock.patch.object(runner, "poll_oracle", oracle), \
                mock.patch.object(runner, "choose_standard", return_value=DecisionResult("error", "capture", None, "m", None, {}, reason="refusal", inference=InferenceEvidence("m", error_code="refusal"))):
            result = asyncio.run(runner._run_task(args, task))
        return result, actions, events

    def test_unknown_delivery_stops_before_second_click(self):
        outcome, actions, events = self.run_fake()
        self.assertEqual(outcome, "unknown")
        self.assertEqual(actions, ["click"])
        attempt = next(event for event in events if event["event"] == "action_attempt")
        step = next(event for event in events if event["event"] == "step")
        self.assertEqual(attempt["attempt_id"], step["attempt_id"])
        self.assertEqual(step["delivery_mode"], "unknown")
        self.assertEqual(step["requested_delivery_mode"], "background")
        self.assertEqual(step["delivery_status"], "uncertain")

    def test_independent_progress_allows_normal_ax_clicks(self):
        outcome, actions, events = self.run_fake(mode="background", progress=True)
        self.assertEqual(outcome, "verified")
        self.assertEqual(actions, ["click"] * 3)
        self.assertEqual(sum(event["event"] == "action_reconciled" for event in events), 3)

    def test_partial_suspected_noop_and_unchanged_state_stop(self):
        for effect, progress in [("partial", True), ("suspected_noop", True), ("unverifiable", False)]:
            with self.subTest(effect=effect, progress=progress):
                outcome, actions, _ = self.run_fake(mode="background", progress=progress, effect=effect)
                self.assertEqual(outcome, "unknown")
                self.assertEqual(actions, ["click"])

    def test_unknown_delivery_goal_can_still_be_verified(self):
        outcome, actions, _ = self.run_fake(mode="unknown", progress=True, initial=2)
        self.assertEqual(outcome, "verified")
        self.assertEqual(len(actions), 1)

    def test_action_timeout_retains_attempt_and_phase_times(self):
        outcome, actions, events = self.run_fake(error=TimeoutError())
        self.assertEqual(outcome, "unknown")
        self.assertEqual(len(actions), 1)
        failure = events[-1]
        self.assertEqual(failure["observation"]["observe_ms"], 123)
        self.assertIsInstance(failure["act_ms"], float)
        self.assertEqual(failure["delivery_status"], "unresolved")

    def test_decision_refusal_retains_observation_time(self):
        outcome, actions, events = self.run_fake(provider="standard-llm")
        self.assertEqual(outcome, "unknown")
        self.assertEqual(actions, [])
        self.assertEqual(events[-1]["observation"]["observe_ms"], 123)
        self.assertEqual(events[-1]["inference"]["error_code"], "refusal")

    def test_probability_rejection_retains_selection_evidence(self):
        decision = DecisionResult("selected", "capture", "ax:button:increment", "m", .55,
                                  {"ax:button:increment": .55, "abstain": .45},
                                  inference=InferenceEvidence("m"))
        with mock.patch.object(runner, "choose", return_value=decision), \
                mock.patch.object(runner, "validate_dispatch", side_effect=ValueError("probability_gate")):
            outcome, actions, events = self.run_fake(provider="openai-decisions")
        self.assertEqual((outcome, actions), ("unknown", []))
        self.assertEqual(events[-1]["reason"], "probability_gate")
        self.assertEqual(events[-1]["inference"]["error_code"], "probability_gate")
        self.assertEqual(events[-1]["selected_probability"], .55)
        self.assertAlmostEqual(events[-1]["selection_margin"], .1)


class RefusalRegressionTests(unittest.TestCase):
    def test_explicit_provider_refusals_keep_metadata_without_scores(self):
        cases = [(OpenAIDecisionModel, {"answers": [{"name": "candidate", "type": "refusal", "refusal": "sensitive"}]}),
                 (MicrosoftDecisionModel, {"answers": {"candidate": {"type": "refusal", "refusal": "sensitive"}}})]
        for model, body in cases:
            tx, calls = transport({**body, "model": "returned", "usage": {"input_tokens": 12}})
            self.addCleanup(tx.client.close)
            result = choose(model(tx), request())
            self.assertEqual(result.kind, "error")
            self.assertEqual(result.inference.error_code, "refusal")
            self.assertEqual(result.inference.input_tokens, 12)
            self.assertEqual(result.inference.returned_model, "returned")
            self.assertIsNone(result.confidence)
            self.assertEqual(result.probabilities, {})
            self.assertNotIn("sensitive", json.dumps(result.to_wire()))

    def test_responses_refusal_is_distinct(self):
        tx, _ = transport({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "sensitive"}]}]})
        self.addCleanup(tx.client.close)
        with self.assertRaises(ProviderError) as caught:
            choose_standard(tx, request(), [])
        self.assertEqual(caught.exception.inference.error_code, "refusal")
        self.assertEqual(caught.exception.inference.request_count, 1)


if __name__ == "__main__": unittest.main()
