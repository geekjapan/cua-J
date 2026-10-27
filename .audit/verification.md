# Import verification

Source is trycua/cua at bb507dd282c53b872edd5cabff81860fcaa746bf. UPSTREAM.json records 1,392 imported files. Their original hashes match except the documented Rust toolchain pin. The root pre-existing initialization history remains intact.

## Verified

- CLI and Rust SDK build with Rust 1.95.0 and the unchanged upstream Cargo.lock.
- `cargo +1.95.0 test --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver-core -p cua-driver-contract -p cua-driver-sdk` exits 0. Its result summaries total 1,335 passed and 2 ignored. The ignored cases require disposable Chrome consent or are a documentation example; neither is counted as passing.
- `cargo +1.95.0 test --locked --manifest-path libs/cua-driver/rust/Cargo.toml -p cua-driver -- --test-threads=1` exits 0. CLI unit tests and integration binaries pass. Explicit desktop tests remain ignored.
- The ordinary parallel CLI run failed one pacman ownership unit test after 354 passed. The exact test passed alone and the complete serial run passed. Cause remains unconfirmed. This is retained as a nondeterminism concern, not repaired by changing upstream product code.
- `python3 scripts/verify-runtime.py` exits 0. The public CLI lists tools, describes get_screen_size, starts a dedicated daemon and reads the native display as 2560 x 1440 with scale 2.0. The owned process group is absent afterward and evidence survives scratch cleanup.
- `python3 scripts/verify-formal.py --mutation` exits 0 with Z3 5.1.0. The correct bounded model returns unsat and the broken model sat. Actual source executes 10,000 four-operation traces. Baseline has 20 passing tests, removal of production admission consumption fails three tests including trace 0 step 1, and restoration returns to 20 passing tests. The final capture source hash matches upstream.
- Both generated skills pass the installed skill-creator validator. Helpers are executable. Staged whitespace checks pass.

## Limits and review

Only native macOS display reading is certified. Screenshot capture, input injection, accessibility journeys, app-bundle TCC identity, Windows/Linux, optional perception models, and Python/TypeScript package builds are not certified. An unbundled daemon warns that background focus operations lack a valid WindowServer connection. This run does not claim those operations work.

The model bounds one capture and four sequential operations. Mutex linearization is assumed from source inspection. No concurrent-memory or arbitrary-input proof is claimed. SIGTERM and SIGINT restoration handling is implemented, with a persistent backup and signals blocked during restoration; interruption fault injection itself was not executed. SIGKILL still requires manual recovery from the backup.

Independent native research/advisor/review agents checked dependency closure, verification design, comments and helper safety. Review found separate admission paths and two cleanup/restoration gaps; the root corrected them. Comment review found zero removable comments in the new local code. All seats use GPT-family native routes; no cross-family review is claimed. The deslop plugin was unavailable; native review was used and is not represented as a deslop invocation.

The FSL license for the telemetry teleport catalog is retained. This import is not MIT-only. No push, remote publication, installer execution or upstream-history rewrite was performed. Build outputs and raw proof artifacts are ignored local files.

## Local evidence hashes

- `artifacts/formal.log` has SHA-256 `db0007d1ad5a3040b81995061da9a74d5cfd3ad97a4a8683bc18d397a8bdb2f3`.
- `artifacts/tests.log` has SHA-256 `fb2981b928f1f284d41206fc2c4848eb8e032dbdfe136821e8e45ef561442503`.
- `artifacts/cli-tests.log` has SHA-256 `151493ac7005293841d7e34b71b5fd3c36a20e637e4f6e7ae3bc31e275d2c6ee`.
- `artifacts/cli-failure-recheck.log` has SHA-256 `5acfb1364564e4f3d659083928ca5cdc4c5839511f6e2c8745e52d6e5f9b890f`.
- `artifacts/cli-tests-serial.log` has SHA-256 `359d2ac7bce38cc822abf8533e17ec478cd08de54243a6d0a7644366fabd6f1c`.
- `artifacts/runtime/result.json` has SHA-256 `e7cddd9284afd4e04ab6d19ef1e05e0c18c623d52aa6ea0a7006dd4126bd9895`.
- `artifacts/runtime/cleanup.json` has SHA-256 `154995c1b849e0f7acbccc7b2f8c05fcd4219837d71e8ee0fea2f3646a765a13`.
