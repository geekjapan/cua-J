---
name: cua-j
description: Set up and operate the cua-J fork of Cua Driver for native GUI or supported browser tasks, including its OpenAI Decisions and Microsoft Decision-1 examples.
license: MIT
---

# cua-J

Use the executable built from [geekjapan/cua-J](https://github.com/geekjapan/cua-J) and the guides bundled with that runtime.

## Connect to Driver

Resolve the selected executable from `CUA_DRIVER_BIN` or `command -v cua-driver`. Run its `--version` and `list-tools` commands. A version number alone does not identify the runtime behind an existing daemon.

For a missing executable or a new source installation, follow the [source-build instructions](https://github.com/geekjapan/cua-J/blob/main/README.md#install-on-macos). Keep the checkout path for the Python examples. Skill installation supplies these instructions; the Driver executable, OS grants and API credentials require their own setup.

Configure the MCP client with that executable's absolute path. On macOS, use `args: ["mcp", "--direct"]` to run this fork's runtime in the MCP process. Bare `mcp` proxies to the installed `CuaDriver.app`. Direct mode uses the launching host's Accessibility and Screen Recording grants and provides no AppKit cursor overlay. Granting those permissions belongs to the user or an explicitly authorized trusted host.

Use the connected tool schemas, or the exact executable's `describe TOOL` command, before supplying unfamiliar arguments.

## Operate one target

Read `get_guide` with `topic: "overview"` for the running Driver's native workflow. Read `topic: "browser"` for browser work. Other topics include `permissions`, `batching`, `menus` and `drag`. Driver returns these read-only guides through the connected MCP tools.

Select the intended application and exact window before input. Observe that target, use its returned element tokens, then verify the user's postcondition from fresh state. A new snapshot can invalidate prior tokens. Keep browser refs bound to the observed target, tab and snapshot. Use the interaction method the user authorized.

Keep background actions within their supported route. A refused background route does not authorize foreground delivery. Use a fresh observation to find another supported action on the same target, or report the unavailable route.

Treat refusals, partial delivery and unknown effects as unresolved until observation establishes what happened. Do not replay an uncertain input blindly. End only this run's session and owned processes when finished.

## Use the decision examples

Run the Python examples from the source checkout, rather than the directory where this skill was installed. From the checkout root, install the locked dependencies and check a mock decision:

```sh
uv sync --frozen --project libs/cua-driver/examples/jev-use
uv run --frozen --project libs/cua-driver/examples/jev-use \
	python libs/cua-driver/examples/jev-use/verify_decision_cli.py --model mock
```

The check requires no credential or desktop action. It returns `selected` with `selected_id: "submit-form"` when successful.

For authorized paid inference, follow the [remote provider instructions](https://github.com/geekjapan/cua-J/blob/main/libs/cua-driver/examples/jev-use/decision-models.md#remote-decision-providers). OpenAI uses `OPENAI_API_KEY`. Microsoft uses `AZURE_ENDPOINT`, `AZURE_DEPLOYMENT_NAME` and either `AZURE_API_KEY` or `AZURE_ENTRA_TOKEN`. Keep credentials in the checkout's ignored `.env.local` with owner-only permissions and pass it through `uv run --env-file .env.local`. The native and website comparison sections describe the isolated fixtures and explicit opt-in required for actual actions.

A decision selects a candidate from the original observation. Recheck its scope, exact arguments, freshness and caller thresholds before dispatch, then verify the effect. `reobserve`, `abstain` and `error` dispatch no input. Finite-choice validation is deterministic; model choices and external UI outcomes have no repeatability guarantee.
