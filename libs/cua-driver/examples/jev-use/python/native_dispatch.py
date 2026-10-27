from __future__ import annotations

import math
import time
from typing import Sequence

from decision_models import DecisionRequest, DecisionResult
from native import NativeObservation
from sources import Candidate


def validate_dispatch(result: DecisionResult, request: DecisionRequest, candidates: Sequence[Candidate],
                      observation: NativeObservation, *, pid: int, window_id: int,
                      observed_at: float, max_age_ms: int, min_probability: float,
                      min_margin: float, require_probabilities: bool = True) -> Candidate:
    if result.kind == "error" or result.selected_id is None:
        raise ValueError("decision_error")
    if (not observation.capture_id or result.capture_id != observation.capture_id
            or request.capture_id != observation.capture_id
            or request.snapshot_id != observation.snapshot_id):
        raise ValueError("observation_binding")
    age_ms = (time.monotonic() - observed_at) * 1000
    if not math.isfinite(age_ms) or age_ms < 0 or age_ms > max_age_ms:
        raise ValueError("observation_expired")
    matches = [c for c in candidates if c.id == result.selected_id]
    if len(matches) != 1:
        raise ValueError("unknown_candidate")
    candidate = matches[0]
    if candidate.tool is None:
        return candidate
    if observation.pid != pid or observation.window_id != window_id:
        raise ValueError("observation_scope")
    if candidate.arguments.get("pid") != pid or candidate.arguments.get("window_id") != window_id:
        raise ValueError("candidate_scope")
    if candidate.source == "ax":
        token = candidate.arguments.get("element_token")
        if (not observation.snapshot_id or candidate.snapshot_id != observation.snapshot_id
                or not isinstance(token, str) or not token
                or not any(e.get("element_token") == token for e in observation.elements)):
            raise ValueError("snapshot_binding")
    elif candidate.source == "visual":
        if candidate.capture_id != observation.capture_id:
            raise ValueError("capture_binding")
    else:
        raise ValueError("unsupported_source")
    if require_probabilities:
        scores = result.probabilities
        if set(scores) != {c.id for c in candidates} or any(
                type(v) not in {float, int} or not math.isfinite(v) or not 0 <= v <= 1
                for v in scores.values()) or abs(sum(scores.values()) - 1) > .02:
            raise ValueError("invalid_probabilities")
        ordered = sorted(scores.values(), reverse=True)
        if (scores[candidate.id] != ordered[0] or ordered[0] < min_probability
                or len(ordered) < 2 or ordered[0] - ordered[1] < min_margin):
            raise ValueError("probability_gate")
    return candidate
