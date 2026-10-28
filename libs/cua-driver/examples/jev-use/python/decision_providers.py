from __future__ import annotations

import json
import math
import os
import time
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from decision_models import DecisionRequest, InferenceEvidence, ModelScores


class ProviderError(ValueError):
    def __init__(self, code: str, inference: InferenceEvidence):
        super().__init__(code)
        self.inference = replace(inference, error_code=code)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


class ProviderTransport:
    def __init__(self, client: httpx.Client, url: str, headers: dict[str, str], model: str):
        self.client, self.url, self.headers, self.model = client, url, headers, model

    def post(self, body: dict[str, Any]) -> tuple[dict[str, Any], InferenceEvidence]:
        started = time.perf_counter()
        evidence = InferenceEvidence(self.model, request_count=1)
        try:
            response = self.client.post(self.url, headers=self.headers, json=body,
                                        follow_redirects=False)
            evidence = replace(evidence, elapsed_ms=round((time.perf_counter()-started)*1000, 2),
                               request_id=response.headers.get("x-request-id"))
            if not 200 <= response.status_code < 300:
                raise ProviderError(f"http_{response.status_code}", evidence)
            result = decode_json(response.content.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("response must be object")
            usage = result.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("usage must be object")
            returned = result.get("model")
            if returned is not None and (not isinstance(returned, str) or not 0 < len(returned) <= 128):
                raise ValueError("invalid model identity")
            details_in = usage.get("input_tokens_details") or {}
            details_out = usage.get("output_tokens_details") or {}
            evidence = replace(evidence, returned_model=returned,
                input_tokens=_count(usage.get("input_tokens")), output_tokens=_count(usage.get("output_tokens")),
                total_tokens=_count(usage.get("total_tokens")),
                cached_tokens=_count(details_in.get("cached_tokens")) if isinstance(details_in, dict) else None,
                reasoning_tokens=_count(details_out.get("reasoning_tokens")) if isinstance(details_out, dict) else None)
            return result, evidence
        except ProviderError:
            raise
        except httpx.TimeoutException:
            code = "timeout"
        except httpx.HTTPError:
            code = "transport_error"
        except (ValueError, TypeError, UnicodeError):
            code = "invalid_response"
        raise ProviderError(code, replace(evidence, elapsed_ms=round((time.perf_counter()-started)*1000, 2))) from None


def provider_transport(provider: str, client: httpx.Client, openai_model: str = "gpt-6-luna") -> ProviderTransport:
    if provider in {"openai-decisions", "standard-llm"}:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ValueError("OPENAI_API_KEY is required")
        path = "responses" if provider == "standard-llm" else "decisions"
        return ProviderTransport(client, f"https://api.openai.com/v1/{path}",
                                 {"Authorization": f"Bearer {key}"}, openai_model)
    endpoint = os.environ.get("AZURE_ENDPOINT", "").strip()
    parts = urlsplit(endpoint)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("AZURE_ENDPOINT must be an HTTPS resource root")
    key = os.environ.get("AZURE_API_KEY", "").strip()
    token = os.environ.get("AZURE_ENTRA_TOKEN", "").strip()
    if not key and not token:
        raise ValueError("AZURE_API_KEY or AZURE_ENTRA_TOKEN is required")
    model = os.environ.get("AZURE_DEPLOYMENT_NAME", "decision-1")
    return ProviderTransport(client, endpoint.rstrip("/")+"/providers/microsoft/v1/systemone",
                             {"api-key": key} if key else {"Authorization": f"Bearer {token}"}, model)


class OpenAIDecisionModel:
    name = "openai-decisions"

    def __init__(self, transport: ProviderTransport):
        self.transport = transport

    def score(self, request: DecisionRequest) -> ModelScores:
        body = {"model": self.transport.model, "input": json.dumps(request.provider_observation()),
                "questions": [{"name": "candidate", "type": "choice", "instructions": request.goal,
                               "choices": [{"value": key, "description": value}
                                           for key, value in request.criteria.items()]}]}
        result, evidence = self.transport.post(body)
        try:
            answers = result["answers"]
            if not isinstance(answers, list) or len(answers) != 1:
                raise ValueError()
            answer = answers[0]
            if isinstance(answer, dict) and answer.get("type") == "refusal":
                raise ProviderError("refusal", evidence)
            if answer.get("name") != "candidate" or answer.get("type") != "choice":
                raise ValueError()
            raw = answer["probabilities"]
            if not isinstance(raw, list):
                raise ValueError()
            probabilities = {}
            for item in raw:
                if not isinstance(item, dict) or not isinstance(item.get("value"), str) or item["value"] in probabilities:
                    raise ValueError()
                probabilities[item["value"]] = item["probability"]
            return _scores(answer, probabilities, evidence, self.transport.model)
        except ProviderError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("invalid_decision", evidence) from None


class MicrosoftDecisionModel:
    name = "microsoft-decision1"

    def __init__(self, transport: ProviderTransport):
        self.transport = transport

    def score(self, request: DecisionRequest) -> ModelScores:
        result, evidence = self.transport.post({"model": self.transport.model,
            "state": json.dumps(request.provider_observation()),
            "questions": {"candidate": {"type": "choice", "instructions": request.goal,
                                         "criteria": request.criteria}}})
        try:
            answers = result["answers"]
            if not isinstance(answers, dict) or set(answers) != {"candidate"}:
                raise ValueError()
            answer = answers["candidate"]
            if isinstance(answer, dict) and answer.get("type") == "refusal":
                raise ProviderError("refusal", evidence)
            if answer.get("type") != "choice" or not isinstance(answer["probabilities"], dict):
                raise ValueError()
            return _scores(answer, answer["probabilities"], evidence, self.transport.model)
        except ProviderError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("invalid_decision", evidence) from None


def _scores(answer: dict[str, Any], probabilities: dict[str, float], evidence: InferenceEvidence, model: str) -> ModelScores:
    if not isinstance(answer.get("choice"), str) or answer["choice"] not in probabilities:
        raise ValueError("invalid choice")
    confidence = answer["confidence"]
    if type(confidence) not in {int, float} or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("invalid confidence")
    return ModelScores(probabilities, evidence.returned_model or model, answer["choice"], confidence, evidence)
