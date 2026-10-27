# cua-J

A history-independent import of the Cua Driver CLI and libraries from [trycua/cua](https://github.com/trycua/cua), upstream revision `bb507dd282c53b872edd5cabff81860fcaa746bf`.

Driver sources, Rust SDK, Python and TypeScript bindings, contract definitions, platform adapters, fixtures and embedded assets are retained. Spaces, VM management, model training and benchmark products are excluded. The only local crate dependency outside Driver is `cua-telemetry`; its two build-time catalogs are vendored using its upstream fallback mechanism.

## Build and verify on macOS

Requires Rust 1.95.0, Xcode command line tools, Python 3 and Z3 5.1.0. Run from the repository root.

```sh
cargo +1.95.0 build --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -p cua-driver-sdk
python3 scripts/verify-runtime.py
python3 scripts/verify-formal.py --mutation
```

The runtime check launches an isolated daemon and reads native display dimensions through the public CLI. It does not inject input or capture the screen. Evidence survives teardown in ignored `artifacts/`.

The formal check covers one capture and every sequence of four operations drawn from ten lifecycle and admission cases. Z3 checks a symbolic lifecycle model. The Rust check exercises 10,000 traces against the actual source. The mutation run removes action consumption temporarily, requires detection and restores the source. Run it without concurrent source edits. This is bounded sequential verification, not a proof of the whole application or concurrent Rust execution.

Project-local instructions and feature maps are in [.cursor/skills/verify-cua-driver](.cursor/skills/verify-cua-driver/SKILL.md) and [.cursor/skills/verify-capture-consumption](.cursor/skills/verify-capture-consumption/SKILL.md). Use `/maintain-verification-skill` and `/maintain-formal-verification-skill` to update them after changes.

## CI

[GitHub Actions](.github/workflows/ci.yml) runs on pushes to `main`, pull requests and manual dispatch. One macOS 26 job builds the CLI and Rust SDK, runs core, contract and SDK tests, runs CLI tests serially, then runs the bounded formal check with mutation detection. CI logs are retained for seven days.

The hosted job does not run `scripts/verify-runtime.py`. Native display verification remains a separate local check using the commands above. CI does not certify desktop interaction or other platforms.

## Import and licenses

[UPSTREAM.json](UPSTREAM.json) records each imported path and SHA-256. The upstream Git history is not imported. The pre-existing local initialization commit is preserved. Upstream contributor copyright notices are retained.

Local changes include the consolidated root license and its packaging input paths, the reduced `libs/cua/Cargo.toml`, the Rust toolchain pin changed from 1.97.1 to the tested 1.95.0, root documentation and ignores, verification helpers and skills, and the bounded test. `.DS_Store` and upstream `CLAUDE.md` instructions are excluded. Product runtime behavior is unchanged. `UPSTREAM.json` lists deliberate modifications to imported files.

[LICENSE](LICENSE) contains the upstream MIT notice and parallel copyright notices for Cua AI, Inc. and geekjapan modifications. Third-party notices and licenses remain beside their assets. The vendored `teleport-targets.json` comes from an FSL-1.1-MIT directory and retains its [source license](libs/cua/crates/cua-telemetry/vendor/LICENSE-teleport). This import as a whole must not be described as MIT-only. Optional perception models are not downloaded by this build.

Upstream component documentation remains under [libs/cua-driver](libs/cua-driver/README.md). Links and release scripts that target the full upstream monorepo are historical upstream instructions; the supported local entry points are the commands above.
