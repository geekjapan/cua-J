---
name: verify-cua-driver
description: Build and drive the Cua Driver CLI and a disposable native macOS daemon, checking catalog, schema and display dimensions after CLI, transport or platform changes.
---

# Verify Cua Driver

## Launch

Run `cargo +1.95.0 build --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -p cua-driver-sdk` from the repository root. Then run `python3 scripts/verify-runtime.py` on a logged-in macOS desktop. This launches the built binary with `serve`, a unique socket and PID file, isolated CUA_DRIVER_RS_HOME and CUA_HOME, telemetry disabled, and no cursor overlay. It passes the supported `--no-permissions-gate` flag to suppress startup prompts; the selected CoreGraphics dimensions read requires no OS permission. This flag does not grant any TCC permission. Readiness requires the owned socket, successful status and successful native `get_screen_size`, not socket existence alone. No credentials or seed data are required. The helper tears down the process it starts.

## Doctor

The helper runs the exact built binary's `--version`, then `status --socket <owned socket> --pid-file <owned PID file>`. Inspect `artifacts/runtime/version.json`, `status.json` and `daemon.log` when a drive fails. Never substitute the installed system driver. A missing native display is a failure for the screen journey.

## Drive

Run `python3 scripts/verify-runtime.py`. It executes the real CLI commands `list-tools`, `describe get_screen_size`, and `call get_screen_size '{}' --socket <owned socket>`. Require a registered tool, a valid schema, a successful daemon response and positive native display dimensions. This recipe runs short-lived CLI commands through captured subprocess streams; the primary product here is a CLI and library, not a web app. Python and TypeScript bindings are retained but need their own language-specific checks.

## Evidence

`artifacts/runtime/` retains argv, stdout, stderr and exit code for each action, daemon logs, native dimensions, the built binary SHA-256 and cleanup status. This proves catalog discovery and a real CoreGraphics read through the daemon transport. It does not prove screenshot permission, keyboard or pointer injection, browser automation, recording or other operating systems. Feature journeys are indexed in [features/README.md](features/README.md). Do not claim those unrun journeys passed. The helper does not install an app or modify preferences. No screenshots or application inventories are captured.

## Cleanup

The helper uses a unique temporary state directory and a new process group. Its finally block sends SIGTERM only to that owned group, waits, and uses SIGKILL only if necessary. It removes its temporary state and confirms evidence survives. Never kill by process name or stop an existing user daemon. Concurrent runs need distinct evidence directories because this helper intentionally uses one fixed artifact directory. Run it once at a time.

## Helpers

`scripts/verify-runtime.py` is executable. Run `python3 scripts/verify-runtime.py` after the build. A failed run still writes daemon logs and cleanup status. The evidence stays ignored by Git to avoid committing machine-specific logs. Use `/maintain-verification-skill` to keep the feature map aligned with changes.
