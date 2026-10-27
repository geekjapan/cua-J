# Daemon lifecycle

## Sub-features

Start an isolated daemon, inspect status and clean up only the owned process.

## How to get to it (user POV)

Run serve with explicit --socket, --pid-file and --no-overlay --no-permissions-gate. Inspect the same paths with status.

## Driving it with the CLI helper

Run `python3 scripts/verify-runtime.py`. Require status.json, result.json and cleanup.json. The helper waits for the owned process to exit and removes its scratch directory.

## Gotchas

Do not drive the default socket. Evidence uses a fixed directory, so runs must be sequential.
