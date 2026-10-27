# Cleanup import workflow

- [x] Read the Principles section of poteto-mode.
- [x] Phase A: Frame
- [x] Phase B: Design the workflow
- [x] Phase C: Run the loop
- [x] Import Driver and its telemetry dependency, preserving licenses and fixtures.
- [x] Build the CLI and SDK, run hermetic tests, exercise the native CLI.
- [x] Generate and execute runtime and formal verification skills.
- [x] Phase D: Keep the audit trail
- [x] Phase E: Verify and hand back

Done means selected sources build, verification skills execute, evidence survives cleanup, and a local import commit exists. Existing history and local .serena state remain intact. No push is requested.

Data shape is a path and SHA-256 manifest of imported files. The Rust crate graph remains upstream's. Verification adds scripts and skills without changing product behavior. Keep the existing layout rather than flattening paths, which would require edits to includes, fixtures and bindings.

Throughput checkpoint. One root owns all writes. Independent read-only investigation checks dependency closure; later review checks scripts and evidence. Native model diversity is unavailable in this harness, so no cross-family review is claimed.
