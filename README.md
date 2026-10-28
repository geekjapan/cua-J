# cua-J

A history-independent import of the Cua Driver CLI and libraries from [trycua/cua](https://github.com/trycua/cua), upstream revision `bb507dd282c53b872edd5cabff81860fcaa746bf`.

Driver sources, Rust SDK, Python and TypeScript bindings, contract definitions, platform adapters, fixtures and embedded assets are retained. Spaces, VM management, model training and benchmark products are excluded. The only local crate dependency outside Driver is `cua-telemetry`; its two build-time catalogs are vendored using its upstream fallback mechanism.

## Install the agent skill

With Node.js 22.20 or later, run this command from the project where you want to use Cua Driver:

```sh
npx skills add geekjapan/cua-J --skill cua-j
```

Choose your coding agent when prompted. For a non-interactive Codex installation:

```sh
npx skills add geekjapan/cua-J --skill cua-j --agent codex --yes
```

The command installs the [cua-j agent skill](skills/cua-j/SKILL.md) into the current project. Add `--global` for a user-wide installation. See the [skills CLI reference](https://github.com/vercel-labs/skills/blob/main/docs/commands.md) for supported agents and options.

The skill guides setup and operation through Driver's bundled guides. Install the Driver executable with the source-build steps below and connect it to your agent through MCP.

## Install on macOS

Install Git, Xcode command line tools and `rustup` first. These commands build this fork from source. Run the remaining commands from the repository root.

```sh
git clone https://github.com/geekjapan/cua-J.git
cd cua-J
rustup toolchain install 1.95.0 --profile minimal
cargo +1.95.0 build --release --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -p cua-driver-sdk
export CUA_DRIVER_BIN="$PWD/libs/cua-driver/rust/target/release/cua-driver"
export PATH="$PWD/libs/cua-driver/rust/target/release:$PATH"
cua-driver --version
cua-driver list-tools
```

The expected version is `0.38.0`. The exports apply to the current shell. To keep them across terminals, add the absolute checkout paths to your shell configuration. If you set `CARGO_TARGET_DIR`, use that output directory instead.

### Connect an MCP client

Use the absolute path printed by `command -v cua-driver`. For clients with an `mcpServers` configuration, add this entry and replace the example path:

```json
{
  "mcpServers": {
    "cua-driver": {
      "command": "/absolute/path/to/cua-J/libs/cua-driver/rust/target/release/cua-driver",
      "args": ["mcp", "--direct"]
    }
  }
}
```

On macOS, `--direct` runs this fork's runtime in the MCP process and uses the launching host's Accessibility and Screen Recording grants. For native control and screenshots, grant those permissions to the terminal or agent host in **System Settings > Privacy & Security**. Bare `cua-driver mcp` instead proxies to the installed `CuaDriver.app`. See [macOS process identity and permissions](libs/cua-driver/README.md#macos-process-identity-and-permissions) for supported app and embedding modes.

## Verify on macOS

These checks use a debug build and require Python 3 and Z3 5.1.0. Run from the repository root.

```sh
cargo +1.95.0 build --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -p cua-driver-sdk
python3 scripts/verify-runtime.py
python3 scripts/verify-formal.py --mutation
```

The runtime check launches an isolated daemon and reads native display dimensions through the public CLI. It does not inject input or capture the screen. Evidence survives teardown in ignored `artifacts/`.

The formal check covers one capture and every sequence of four operations drawn from ten lifecycle and admission cases. Z3 checks a symbolic lifecycle model. The Rust check exercises 10,000 traces against the actual source. The mutation run removes action consumption temporarily, requires detection and restores the source. Run it without concurrent source edits. This is bounded sequential verification, not a proof of the whole application or concurrent Rust execution.

Project-local instructions and feature maps are in [.cursor/skills/verify-cua-driver](.cursor/skills/verify-cua-driver/SKILL.md) and [.cursor/skills/verify-capture-consumption](.cursor/skills/verify-capture-consumption/SKILL.md). Use `/maintain-verification-skill` and `/maintain-formal-verification-skill` to update them after changes.

## CI

[GitHub Actions](.github/workflows/ci.yml) runs on pushes to `main`, pull requests and manual dispatch. An Ubuntu job runs the locked Python decision and controller tests without credentials or desktop input. A macOS 26 job builds the CLI and Rust SDK, runs core, contract and SDK tests, runs CLI tests serially, then runs the bounded formal check with mutation detection. CI logs are retained for seven days.

The hosted job does not run `scripts/verify-runtime.py`. Native display verification remains a separate local check using the commands above. CI does not certify desktop interaction or other platforms.

## Run decisions through Driver

The Python native runner supports OpenAI Decisions, Microsoft Decision-1 and ordinary OpenAI Responses function calls. It binds each selection to the original observation and authorized action arguments. Model confidence does not grant permission. Driver tokens and capture IDs do not prove that an external UI stayed unchanged during inference.

### Set up the Python example

Install Python 3.10 or later and `uv`. Python 3.13.13 and uv 0.11.6 were used for verification. From the repository root, install the locked dependencies and check a synthetic choice without credentials or desktop actions:

```sh
uv sync --frozen --project libs/cua-driver/examples/jev-use
uv run --frozen --project libs/cua-driver/examples/jev-use \
	python libs/cua-driver/examples/jev-use/verify_decision_cli.py --model mock
```

The check prints `"kind": "selected"` and `"selected_id": "submit-form"` on success.

Create or edit the ignored root `.env.local` file with credentials for the provider you use. These commands preserve an existing file:

```sh
touch .env.local
chmod 600 .env.local
```

| Provider | Variables in `.env.local` |
| --- | --- |
| OpenAI Decisions or Responses | `OPENAI_API_KEY` |
| Microsoft Decision-1 | `AZURE_ENDPOINT` with the HTTPS resource root, `AZURE_DEPLOYMENT_NAME` (default `decision-1`), and either `AZURE_API_KEY` or `AZURE_ENTRA_TOKEN` |

Store each value as `NAME=value` on its own line. An Entra token must still be valid when the command runs. To send one synthetic request to OpenAI Decisions, run the following command. It uses the configured account's paid API and performs no desktop action:

```sh
uv run --frozen --project libs/cua-driver/examples/jev-use --env-file .env.local \
	python libs/cua-driver/examples/jev-use/python/choose_decision.py \
	--model openai-decisions \
	< libs/cua-driver/examples/jev-use/fixtures/jev-choice-request-v1.json
```

For Microsoft, use `--model microsoft-decision1` with its variables configured. A successful request returns `cua.decision_choice_v1`; the decision can be `selected`, `reobserve` or `abstain`. An API or response-validation failure returns `error` or a nonzero setup failure.

See [the decision providers and native comparison](libs/cua-driver/examples/jev-use/decision-models.md#remote-decision-providers) for configuration and verification commands. The comparison uses isolated AppKit tasks with an independent state oracle. It does not measure general desktop planning or unrestricted text generation. The TypeScript and existing browser runners retain their upstream provider support.

The [45-trial native comparison](libs/cua-driver/examples/jev-use/benchmarks/2026-10-28-native.md) records measured completion rates, inference and task times, failures and limits. Sanitized trial data and source hashes accompany the report.

The [45-trial public website comparison](docs/benchmarks/2026-10-28-websites.md) records completion, action outcomes and timing on Wikipedia, Python documentation and Hacker News. See [the website setup](libs/cua-driver/examples/jev-use/decision-models.md#compare-actual-public-websites) to repeat it with isolated Chrome profiles and independent completion checks.

## Import and licenses

[UPSTREAM.json](UPSTREAM.json) records each imported path and SHA-256. The upstream Git history is not imported. The pre-existing local initialization commit is preserved. Upstream contributor copyright notices are retained.

Local changes include the consolidated root license and its packaging input paths, the reduced `libs/cua/Cargo.toml`, the Rust toolchain pin changed from 1.97.1 to the tested 1.95.0, root documentation and ignores, verification helpers and skills, and the bounded test. The Python example adds remote decision providers, guarded native execution, a Responses baseline and repeatable native and website comparisons. The Rust Driver implementation remains unchanged. `.DS_Store` and upstream `CLAUDE.md` instructions are excluded. `UPSTREAM.json` lists deliberate modifications to imported files.

[LICENSE](LICENSE) contains the upstream MIT notice and parallel copyright notices for Cua AI, Inc. and geekjapan modifications. Third-party notices and licenses remain beside their assets. The vendored `teleport-targets.json` comes from an FSL-1.1-MIT directory and retains its [source license](libs/cua/crates/cua-telemetry/vendor/LICENSE-teleport). This import as a whole must not be described as MIT-only. Optional perception models are not downloaded by this build.

Upstream component documentation remains under [libs/cua-driver](libs/cua-driver/README.md). Links and release scripts that target the full upstream monorepo are historical upstream instructions; the supported local entry points are the commands above.
