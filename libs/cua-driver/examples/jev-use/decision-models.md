# Closed-candidate decision models

The optional Python `choose_decision.py` interface lets an application choose
between TypeSafe Jev and a local Cua-S1-4B model without giving either model
control of Cua Driver. It is an example-local integration, not a new Driver,
MCP, or cross-language contract. The Python native runner also supports the
remote providers described below. The TypeScript and browser runners retain
their upstream provider support.

## Boundary

The application observes through Driver, constructs an immutable candidate
table, and sends only the goal, capture ID, typed visual regions, bounded
history, and candidate IDs and descriptions to a decision model. The model
returns a score for each supplied ID. The adapter rejects missing, extra,
non-finite, out-of-range, or non-normalized scores, then returns one of four
outcomes: `selected`, `reobserve`, `abstain`, or `error`.

`selected` is not an action result. The application must look up the selected
ID in its original candidate table, re-check any typed action precondition
against the original current-capture observation, dispatch only an authorized
action through Driver, and verify the postcondition from an independent source.
Candidate descriptions are model input, not executable conditions; never parse
them or treat a high model score as authorization. `reobserve`, `abstain`, and
`error` dispatch no action.
The decision response never contains tool names, tool arguments, coordinates,
or screenshot bytes.

For a simple capture-bound click on an exact OCR label, the Python example
provides `action_policy.ExactRegionTextAction` and
`authorize_exact_region_text_action`. The caller creates the policy before
building the candidate table, uses `action.wire_candidate()` as the offered
candidate, and passes the original `parse_visual_regions` result and current
capture ID plus the expected source (exact window PID/window ID or primary
desktop) to the guard after a `selected` response. This means the raw public
Driver tool payload, not the `core.parse_visual_regions` wrapper's
`VisualObservation` object. The guard validates its source, PNG provenance,
coordinate mapping, and bounds before returning screenshot-pixel coordinates.
Only a successful guard returns a point and capture ID to pass to Driver.
Missing, duplicated, stale,
low-confidence, or mismatched region facts refuse the action. This is one
example caller policy, not a generic authorization engine or a Driver feature;
other action types need their own typed checks. The helper does not dispatch a
click or verify its effect. The caller must pass the returned screenshot-pixel
point to the corresponding window or desktop Driver action with the same
`capture_id`. Driver applies the capture's coordinate mapping
once; the caller must not map the returned point again or reuse it against
another target.

The input is the existing `cua.jev_choice_request_v1` request accepted by
`choose_action.py`. The output is a separate
`cua.decision_choice_v1` response with `kind`, `capture_id`, `selected_id`, `model`,
`confidence`, `probabilities`, and `reason`. This output is not a drop-in
replacement for the older `cua.jev_choice_v1` response; callers must handle
all four outcome kinds and match `capture_id` to the current observation.
Tied top scores are non-actionable errors. For a `selected` outcome, callers
should also apply their own minimum score and margin before dispatching an
action. `confidence` preserves the provider's reported value when Jev supplies
one; with S1 and mock it is the selected option probability. For a
model-independent threshold, use `probabilities[selected_id]` instead.

Malformed model output and lazy model-loading failures return `kind: "error"`
with a non-secret `reason: "model_error"`. Missing local S1 paths or a failure
to initialize the TypeSafe client stop the CLI with a generic nonzero setup
error and no JSON response. Never treat a missing response as `selected`.

## Run a bounded request

The deterministic mock requires no model weights or credential:

```bash
uv run --frozen python/choose_decision.py --model mock \
  < fixtures/jev-choice-request-v1.json
```

To verify the expected fixture choice without printing the full response, run
`uv run --frozen verify_decision_cli.py --model mock`. The same verifier accepts
`--model jev` or `--model s1` after configuring that provider. It checks the
response schema, capture ID, complete candidate set, and selected ID. It does
not dispatch a Driver action or prove live desktop behavior.

The separate negative fixture has a `Save` observation but an action candidate
that requires `Send`. To check a live provider's abstention without executing
the candidate, run `uv run --frozen verify_decision_cli.py --model jev
--fixture negative --expected-id abstain` in an authorized local environment.
The mock intentionally chooses the first action candidate and is not an oracle
for this negative case.

The TypeSafe Jev path uses the example's official SDK and reads its credential
from the local environment. Run it only with data you intend to send to that
service. Do not put `TYPESAFE_API_KEY` in CI or GitHub Actions:

```bash
uv run --frozen python/choose_decision.py --model jev \
  < fixtures/jev-choice-request-v1.json
```

The S1 path runs locally and needs the separately installed `cua-s1` Python
package with its `four-b` inference extra, which supplies PyTorch, PEFT, and
Transformers 5. From the repository root, create that environment from the
checked-in lock with
`uv sync --frozen --project libs/cua-s1/python --extra four-b --extra pdf --group test`
(Transformers 5.17.0 and torch 2.14.0), then run this chooser with
`libs/cua-s1/python/.venv/bin/python`. This is a local source recipe, not a
published package or a guarantee that every allowed dependency version loads
the model.

The published `cua-ai/cua-s1-4b-0.2` artifact is a PEFT adapter, not a
standalone model. Download the base revision
`851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a` and adapter revision
`16818868b0cc7813808aae4e87b417657046ab79` to local directories, then set
`S1_BASE_MODEL_PATH` and `S1_ADAPTER_PATH` to them. `S1_DEVICE` defaults to
`cpu` and `S1_DTYPE` to `float16`. The Cua-S1 README's
[Get the weights and run inference](../../../cua-s1/README.md#get-the-weights-and-run-inference)
section has the pinned download commands for every checkpoint, hardware
guidance, and the expected smoke output. The 4B base needs more than 9.3 GB
of memory, so 8 GB hosts cannot run this path.

```bash
S1_DEVICE=cpu S1_DTYPE=float16 \
  /path/to/repo/libs/cua-s1/python/.venv/bin/python \
  python/choose_decision.py --model s1 \
  < fixtures/jev-choice-request-v1.json
```

On Apple silicon, use `S1_DEVICE=mps` with the default `float16` or with
`bfloat16`. `cua_s1` loads `mps` weights sequentially, because Transformers'
concurrent loader crashed or hung with `mps` and `float16` (#4198).

The S1 text adapter renders OmniParser regions, including confidence and
interactivity, as text and labels the result
`Visual-region-derived observation`. It is not a genuine accessibility tree,
even though the underlying S1 prompt still titles this text field
`Accessibility tree:`. The reserved options are represented as closed-choice
decisions; their accuracy is not established by S1's element/action training.
S1's one-letter-per-option readout supports at most 26 candidates, including
`reobserve` and `abstain`; a larger validated request returns `error` with
`reason: "option_limit"` and never silently truncates the table. The Jev
request schema permits up to 32 candidates. The S1 multimodal adapter requires
an existing local screenshot path and an explicit screenshot capture ID equal
to the request's `capture_id`. The caller must supply the image from that
capture; the adapter checks identity but cannot authenticate image contents.
The path and image are never added to a TypeSafe request.

`choose_decision.py --model s1` loads the text adapter by default. Select the
multimodal adapter with `--s1-modality multimodal` (or `S1_MODALITY=multimodal`)
and pass the capture's image with `--screenshot <png>` and
`--screenshot-capture-id <capture_id>`; the chooser returns `error` when the
capture ID differs from the request. `verify_decision_cli.py --model s1
--screenshot <png>` runs the same check against a fixture, binding the image to
the fixture's capture ID.

The response's `model` field names the checkpoint as
`<adapter>[@<revision>]:<modality>`, for example
`cua-s1-4b-0.2@16818868b0cc7813808aae4e87b417657046ab79:text`. The adapter name
and revision come from a Hugging Face cache snapshot path or from the metadata
that `hf download --local-dir` writes; otherwise the directory name is used
without a revision. Set `S1_ADAPTER_ID` (for example `cua-ai/cua-s1-4b-0.2`) and
`S1_ADAPTER_REVISION` to state them explicitly. The base model is not part of
this identity, so record `S1_BASE_MODEL_PATH`'s revision alongside the
evidence.

The chooser supports only Cua-S1-4B PEFT adapters (`cua-s1-4b-0.2` and
`cua-s1-4b-0.1`). `cua-s1-nano-0.1` and `cua-s1-forms` are byte-level scorers
with a different input format; `S1_ADAPTER_PATH` pointing at them fails setup.

## Verification scope

The focused fake-model tests cover the selection contract, reserved outcomes,
malformed scores, the 26-option boundary, and absence of screenshot and action
arguments in the TypeSafe request. A local pinned-weight S1 text inference
selected the expected `submit-form` candidate from the synthetic fixture
request. That is a runtime smoke, not evidence that S1 matches Jev's decision
quality or that the S1 path has been run end to end on a desktop.

In a synthetic mismatch where the observation said `Save` but the only action
condition required `Send`, Jev selected `abstain`. Before the S1 text prompt
included region confidence and interactivity, S1 selected non-actionable
`reobserve`. In one pinned-weight rerun with those fields present, S1 selected
`abstain` (probability 0.435 versus 0.384 for `reobserve`). Both earlier and
revised outputs prevented an action; one corrected result is not a reliability
estimate. A fresh primary-desktop negative control exposed the opposite
polarity: packaged OmniParser observed `Send`, the sole action description
required exact `Save`, and S1 selected that action with probability 0.777.
The private probe dispatched no click, and the fixture stayed at zero actions.
This is a model-quality counterexample, not a passing negative decision row.
The typed caller policy refuses the same mismatch in deterministic tests.
Keep an independent fixture oracle and a caller-owned score and margin policy
for future desktop comparisons; a threshold alone cannot establish that an
action condition is true.

## Remote decision providers

The Python chooser and native runner accept `openai-decisions` and
`microsoft-decision1`. Each adapter reuses a pooled HTTP client, makes one
attempt per decision, and refuses redirects. Invalid responses and provider
refusals dispatch no input. Optional `inference` metadata records model identity,
timing, available token counts, and safe error codes. Missing usage stays unknown.

OpenAI uses `OPENAI_API_KEY`, the fixed `/v1/decisions` endpoint, and
`gpt-6-luna`. Microsoft uses `AZURE_ENDPOINT`, an HTTPS Foundry resource root,
and `AZURE_DEPLOYMENT_NAME`, which defaults to `decision-1`. Set either
`AZURE_API_KEY` or `AZURE_ENTRA_TOKEN`. Its endpoint path is
`/providers/microsoft/v1/systemone`. Microsoft accepts text rather than
screenshots. Both adapters receive the same textual observation.

From the repository root, install the locked dependencies and run a bounded
request. This makes a paid API call but performs no desktop action. Keep the
OpenAI key in the ignored root `.env.local` file with owner-only permissions.

```sh
uv sync --frozen --project libs/cua-driver/examples/jev-use
uv run --frozen --project libs/cua-driver/examples/jev-use --env-file .env.local \
	python libs/cua-driver/examples/jev-use/python/choose_decision.py \
	--model openai-decisions \
	< libs/cua-driver/examples/jev-use/fixtures/jev-choice-request-v1.json
```

Run the credential-free tests from this example directory with
`uv run --frozen python -m unittest discover -s python/tests`.

## Compare native decision providers with ordinary tool calls

`benchmark_native.py` compares both decision APIs with `standard-llm`, which
generates tool names and arguments through ordinary OpenAI Responses function
calls. The executor requires an exact match to an authorized action from the
original observation. The baseline has no fabricated probabilities. Its
default model is `gpt-6-luna`, with reasoning disabled.

See the [45-trial comparison results](benchmarks/2026-10-28-native.md) for measured
completion rates, timing ranges and limits.

Build from the repository root:

```sh
cargo +1.95.0 build --release --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -p cua-driver-sdk
bash libs/cua-driver/tests/fixtures/build/macos.sh --only appkit
```

In one terminal, start an owned daemon with private state and an explicit
socket. Embedded mode uses the launching host's macOS permissions.
`--no-permissions-gate` suppresses startup prompts and does not grant permission.

```sh
mkdir -p artifacts/decision-daemon
CUA_DRIVER_RS_HOME="$PWD/artifacts/decision-daemon/home" \
CUA_HOME="$PWD/artifacts/decision-daemon/cua" CUA_DRIVER_RS_TELEMETRY_ENABLED=0 \
	libs/cua-driver/rust/target/release/cua-driver serve --embedded \
	--socket "$PWD/artifacts/decision-daemon/driver.sock" \
	--pid-file "$PWD/artifacts/decision-daemon/driver.pid" \
	--no-overlay --no-permissions-gate
```

In another terminal at the repository root, configure authentication and run
the comparison. Replace the resource placeholder. The Azure CLI command
obtains an Entra token without printing it.

```sh
export CUA_DRIVER_BIN="$PWD/libs/cua-driver/rust/target/release/cua-driver"
export AZURE_ENDPOINT="https://<resource>.services.ai.azure.com"
export AZURE_DEPLOYMENT_NAME="decision-1"
export AZURE_ENTRA_TOKEN="$(az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken --output tsv)"
uv run --frozen --project libs/cua-driver/examples/jev-use --env-file .env.local \
	python libs/cua-driver/examples/jev-use/benchmark_native.py \
	--driver-socket "$PWD/artifacts/decision-daemon/driver.sock" \
	--repetitions 5 --output-dir artifacts/decision-comparison
```

This command makes paid API calls and injects input into fresh AppKit fixture
processes. Run it only with explicit authorization and host permissions. Stop
the daemon in the first terminal when finished. Output directories must be new.

The default schedule rotates three providers across three tasks, five times
each. An independent state file checks the goal and rejects distractor actions.
Failures and timeouts remain in the denominator. Successful latency summaries
are separate from all-trial wall times. Interrupted delivery remains unknown
until evidence resolves it.

All variants receive the same actual AX text, goal, and performed-action
history. Benchmark mode removes declared-step progress and next-action hints,
and uses task-independent candidate retention. The note is deliberately public
synthetic text. Outside benchmark mode, secret parameters retain their redaction.

This measures bounded native tasks rather than general GUI planning or
unrestricted text generation. Microsoft and OpenAI use different models.
Confidence thresholds do not grant permission. A valid token or capture ID does
not prove that the UI stayed unchanged during inference. Resolve uncertain
effects from an independent postcondition before continuing.

The integration uses request and validation patterns from
[geekjapan/jev-ultrafast](https://github.com/geekjapan/jev-ultrafast/blob/f4d66bde5a3301bee8bb19b0e4a4be318fb05074/jev_ultrafast/model.py)
as a reference. Its OpenAI implementation is on a branch separate from its
Azure-only main revision. No sibling package is imported at runtime.

## Compare actual public websites

`benchmark_web.py` uses the same providers through Cua Driver's typed browser
tools. It starts a fresh Driver-owned Chrome profile per trial. It searches
Wikipedia for Solar eclipse, opens Python's `pathlib.Path.read_text`
documentation, and opens the first new Hacker News story's discussion.

Use the release Driver daemon and authentication environment above. From the
repository root, run this command only with authorization for paid API calls
and interaction with these public sites.

```sh
uv run --frozen --project libs/cua-driver/examples/jev-use --env-file .env.local \
	python libs/cua-driver/examples/jev-use/benchmark_web.py \
	--allow-live-sites --driver-socket "$PWD/artifacts/decision-daemon/driver.sock" \
	--repetitions 5 --timeout 180 --max-decisions 8 \
	--output-dir artifacts/website-comparison
```

The default schedule has 45 trials with rotating provider order. Each trial uses
snapshot-bound browser refs, trusted background clicks and the same task query.
The Responses baseline generates full tool arguments. Decisions selects an
enumerated candidate and must pass the unchanged 0.6 probability and 0.1 margin
gates. Neither receives correctness labels or oracle results.

Completion checks the target URL path or anchor and page title. Hacker News also requires
the first observed story's independently resolved submission ID. Task time
includes observation, inference, dispatch and verification after start-page
setup. Successful times are separate from all-trial wall times. Action outcomes
retain verified progress, preparation, wrong destination, no change, refusal,
error and unknown. These measure the fixed controller policy, not intrinsic
model accuracy. Changing live content and host load limit comparisons.

Verified action-cycle time starts before candidate construction and inference
and ends after the action's observed postcondition. Per-observation waiting is
capped at 60 seconds within the common 180-second task budget.

Raw snapshots, event logs, usage and source hashes remain in the new output
directory. The runner closes its owned browser and stops the schedule after
interruption, incomplete inference evidence or unsuccessful cleanup. It does
not modify a personal browser profile. Stop the owned daemon when finished.
