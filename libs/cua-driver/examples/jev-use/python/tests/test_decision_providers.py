from __future__ import annotations
import json
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from decision_models import DecisionRequest, DecisionResult, choose
from decision_providers import MicrosoftDecisionModel, OpenAIDecisionModel, ProviderError, ProviderTransport
from native import NativeObservation
from native_dispatch import validate_dispatch
from sources import Candidate
from standard_llm import choose_standard


def request():
    return DecisionRequest("Reach three", "capture", (), (),
        ({"id": "increment", "description": "Increment"}, {"id": "abstain", "description": "Stop"}),
        snapshot_id="snapshot", observation_text="Counter: 2")


def transport(body, status=200):
    calls = []
    def respond(req):
        calls.append(json.loads(req.content))
        return httpx.Response(status, text=json.dumps(body), headers={"x-request-id": "synthetic"})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return ProviderTransport(client, "https://provider.invalid/decisions", {}, "requested"), calls


def microsoft_body():
    return {"model": "microsoft-decision-1", "answers": {"candidate": {
        "type": "choice", "choice": "increment", "confidence": .9,
        "probabilities": {"increment": .9, "abstain": .1}}},
        "usage": {"input_tokens": 50, "output_tokens": 1}}


class ProviderTests(unittest.TestCase):
    def test_microsoft_named_map_and_nullable_usage(self):
        tx, calls = transport(microsoft_body()); self.addCleanup(tx.client.close)
        result = choose(MicrosoftDecisionModel(tx), request())
        self.assertEqual(result.kind, "selected")
        self.assertEqual(result.inference.input_tokens, 50)
        self.assertIsNone(result.inference.total_tokens)
        self.assertEqual(result.inference.returned_model, "microsoft-decision-1")
        self.assertEqual(calls[0]["questions"]["candidate"]["criteria"], request().criteria)
        self.assertIn("Counter: 2", calls[0]["state"])

    def test_openai_named_list(self):
        answer = {"name": "candidate", "type": "choice", "choice": "increment", "confidence": .9,
                  "probabilities": [{"value": "increment", "probability": .9}, {"value": "abstain", "probability": .1}]}
        tx, calls = transport({"model": "returned", "answers": [answer]}); self.addCleanup(tx.client.close)
        self.assertEqual(choose(OpenAIDecisionModel(tx), request()).selected_id, "increment")
        self.assertIn("Counter: 2", calls[0]["input"])
        answer["probabilities"].append({"value": "increment", "probability": 0})
        tx, _ = transport({"answers": [answer]}); self.addCleanup(tx.client.close)
        self.assertEqual(choose(OpenAIDecisionModel(tx), request()).kind, "error")

    def test_malformed_refusal_and_tie_never_select(self):
        bodies = []
        for probability in [True, float("nan"), -1, 2]:
            body = microsoft_body(); body["answers"]["candidate"]["probabilities"]["increment"] = probability; bodies.append(body)
        body = microsoft_body(); body["answers"]["candidate"]["choice"] = "other"; bodies.append(body)
        body = microsoft_body(); body["answers"]["candidate"]["type"] = "refusal"; bodies.append(body)
        body = microsoft_body(); body["answers"]["candidate"]["probabilities"] = {"increment": .5, "abstain": .5}; bodies.append(body)
        for body in bodies:
            with self.subTest(body=body):
                tx, _ = transport(body); self.addCleanup(tx.client.close)
                result = choose(MicrosoftDecisionModel(tx), request())
                self.assertEqual(result.kind, "error")
                self.assertEqual(result.inference.request_count, 1)
                self.assertIsNotNone(result.inference.error_code)

    def test_duplicate_json_is_rejected(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text='{"model":"a","model":"b"}')))
        self.addCleanup(client.close)
        result = choose(MicrosoftDecisionModel(ProviderTransport(client, "https://p.invalid", {}, "m")), request())
        self.assertEqual(result.inference.error_code, "invalid_response")

    def test_timeout_http_and_redirect_are_single_attempt(self):
        for status in [401, 429, 500, 307]:
            tx, calls = transport({"secret": "never log me"}, status); self.addCleanup(tx.client.close)
            result = choose(MicrosoftDecisionModel(tx), request())
            self.assertEqual(len(calls), 1)
            self.assertEqual(result.inference.error_code, f"http_{status}")
            self.assertNotIn("secret", json.dumps(result.to_wire()))
        def timeout(_):
            raise httpx.ReadTimeout("secret-url-token")
        client = httpx.Client(transport=httpx.MockTransport(timeout)); self.addCleanup(client.close)
        result = choose(MicrosoftDecisionModel(ProviderTransport(client, "https://p.invalid", {}, "m")), request())
        self.assertEqual(result.inference.error_code, "timeout")
        self.assertNotIn("secret-url", json.dumps(result.to_wire()))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.candidate = Candidate("increment", "Increment", "click", {"pid": 1, "window_id": 2,
            "element_token": "entire:token:1", "delivery_mode": "background"}, source="ax", snapshot_id="snapshot")
        self.candidates = [self.candidate, Candidate("abstain", "Stop", None, {})]
        self.observation = NativeObservation(1, 2, "snapshot", "capture", (0, 0, 10, 10),
            ({"element_token": "entire:token:1"},), True, False, None, "Counter: 2")
        self.result = DecisionResult("selected", "capture", "increment", "m", .9, {"increment": .9, "abstain": .1})

    def dispatch(self, **overrides):
        options = dict(pid=1, window_id=2, observed_at=time.monotonic(), max_age_ms=30000, min_probability=.6, min_margin=.1)
        options.update(overrides)
        return validate_dispatch(self.result, request(), self.candidates, self.observation, **options)

    def test_exact_snapshot_scope_token_and_age(self):
        self.assertIs(self.dispatch(), self.candidate)
        for change in [dict(pid=3), dict(window_id=3), dict(observed_at=time.monotonic()-31)]:
            with self.assertRaises(ValueError): self.dispatch(**change)
        for candidate in [replace(self.candidate, snapshot_id="other"),
                          replace(self.candidate, arguments={**self.candidate.arguments, "element_token": "token:1"})]:
            self.candidates[0] = candidate
            with self.assertRaises(ValueError): self.dispatch()

    def test_probability_threshold_and_margin(self):
        for probabilities in [{"increment": .59, "abstain": .41}, {"increment": .54, "abstain": .46},
                              {"increment": float("nan"), "abstain": .1}]:
            self.result = replace(self.result, probabilities=probabilities)
            with self.assertRaises(ValueError): self.dispatch()

    def test_standard_matches_whole_generated_call_without_fake_scores(self):
        for mutation in [{}, {"element_token": "token:1"}, {"pid": True}, {"value": "unauthorized"}]:
            tx, calls = transport({"status": "completed", "model": "returned", "output": [
                {"type": "function_call", "name": "click", "arguments": json.dumps({**self.candidate.arguments, **mutation})}]})
            self.addCleanup(tx.client.close)
            if mutation:
                with self.assertRaises(ProviderError): choose_standard(tx, request(), self.candidates)
            else:
                result = choose_standard(tx, request(), self.candidates)
                self.assertEqual(result.selected_id, "increment")
                self.assertEqual(result.probabilities, {})
                self.assertIsNone(result.confidence)
                self.assertEqual(calls[0]["reasoning"], {"effort": "none"})
                prompt = json.loads(calls[0]["input"])
                self.assertIn("Counter: 2", prompt["observation"]["visible_text"])
                self.assertNotIn("candidates", prompt)
                self.assertNotIn("tool", prompt["controls"][0])


class BenchmarkInputTests(unittest.TestCase):
    def test_public_note_goal_and_actual_history_without_step_hints(self):
        from run_native import parse_args, task_from_args
        from native_tasks import native_choice_request
        from sources import NativeAccessibilitySource
        from tasks import TaskSources
        args = parse_args(["--task", "appkit-save-note", "--pid", "1", "--state-file", "/tmp/unused",
                           "--note-text", "public synthetic note", "--benchmark-input"])
        task = task_from_args(args)
        self.assertEqual(task.cap_order, "depth_first")
        self.assertIn("public synthetic note", task.goal)
        self.assertFalse(task.parameters[0].secret)
        fixture = Path(__file__).resolve().parents[2] / "fixtures/native/appkit-window-state-initial-v1.json"
        payload = json.loads(fixture.read_text())
        observation = NativeObservation.from_window_state(payload, expected_pid=payload["pid"], expected_window_id=payload["window_id"])
        sources = TaskSources(ax=NativeAccessibilitySource.from_observation(observation, "macos", redact=task.redact_text))
        history = [task.history_entry(1, "ax:button:save-note", outcome="desired step complete")]
        wire = native_choice_request(task, sources, task.plan(sources), history, benchmark_input=True)
        self.assertNotIn("progress", wire)
        self.assertEqual(wire["history"][0]["outcome"], "action delivered")
        self.assertNotIn("still requires", json.dumps(wire))
        self.assertNotIn("done yet", json.dumps(wire))
        self.assertNotIn("desired step complete", json.dumps(wire))

    def test_legacy_note_redaction_and_invalid_thresholds(self):
        from run_native import parse_args, task_from_args
        args = parse_args(["--task", "appkit-save-note", "--pid", "1", "--state-file", "/tmp/unused"])
        self.assertTrue(task_from_args(args).parameters[0].secret)
        import contextlib
        import io
        for flag, value in [("--min-probability", "nan"), ("--min-margin", "2"), ("--max-observation-age-ms", "0")]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(["--task", "appkit-counter", "--pid", "1", "--state-file", "/tmp/unused", flag, value])


if __name__ == "__main__": unittest.main()
