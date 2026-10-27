---
name: verify-capture-consumption
description: Check Cua Driver capture action admission after edits to capture_registry.rs, including single consumption, binding rejection, retirement and expiry.
---

# Verify capture action admission

A capture ID must authorize at most one accepted action. Reuse could apply an action using an already consumed screenshot.

## Run

From the repository root, run `python3 scripts/verify-formal.py`. Use a POSIX host with Rust 1.95.0 and Z3 5.1.0. The script checks Z3 is callable, checks the bounded symbolic model and its broken variant, then runs the real Rust source tests. Run `python3 scripts/verify-formal.py --mutation` after changing this skill or its tests. It temporarily removes consumption from the real `admit_action`, requires a test counterexample, restores the exact bytes in a finally block, and reruns the tests. Do not interrupt a mutation run or edit the covered source concurrently. A persistent pre-mutation backup remains at `artifacts/capture_registry.rs.before-mutation`. Normal exceptions, Ctrl-C and SIGTERM restore the source. After SIGKILL or a machine crash, compare that backup with the current source and recover the removal immediately before `Ok(admission)` before continuing. Concurrent source edits cause restoration to stop rather than overwrite them.

## Coverage and bounds

Covered source is `libs/cua-driver/rust/crates/cua-driver-core/src/capture_registry.rs`. The implementation mapping is `CaptureService::publish` for initialization, `CaptureService::admit_action` and `CaptureRegistry::admit_action` for checked consumption, `retire_session` for invalidation, and `MonotonicClock` for expiry. The bounded Rust test is `tests/capture_consumption_bounded.rs` in that crate. It includes the actual source by path because the library keeps this module crate-private. It does not reimplement admission.

The symbolic state is one published capture, live or unavailable, and a count of admissions. Ten operations cover valid consumption, four incorrect binding or target cases, retirement, expiry, a perception read, invalid coordinates and an incorrect native frame. Z3 symbolically explores every four-operation sequence. The Rust check executes all 10,000 corresponding traces and asserts every result against the lifecycle state. Reads and refused attempts leave the capture usable. There is no republishing or eviction in this scope. Operations are serialized because the implementation holds one mutex over checks and removal. This is bounded verification of a sequential abstraction, not a proof of Rust memory safety or concurrent scheduling. It does not prove authorization outside this registry, image transformation mathematics, or desktop input delivery.

## Interpret and maintain

The good model returns `unsat` for two admissions. The model without consumption returns `sat`. The real-code mutation must fail on a trace with repeated valid admission. A failure on unchanged source is a finding. Keep its trace number and assertion; do not weaken the property to pass. For a suspected modeling error, compare the transition against the named Rust operation before changing the model.

Sync source revision is upstream `bb507dd282c53b872edd5cabff81860fcaa746bf`, recorded with per-file hashes in `UPSTREAM.json`. The imported capture_registry.rs is unchanged at verification. Move the sync revision to a new local commit only after confirming the model still describes the named code. Use `/maintain-formal-verification-skill` after covered changes that were not rechecked.
