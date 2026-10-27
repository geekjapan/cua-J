# cua-J

This checkout retains Cua Driver and its minimal local dependency closure. Keep product implementation changes separate from upstream import bookkeeping. Preserve upstream and third-party licenses. `UPSTREAM.json` records the source revision and original file hashes; record deliberate deviations in README.md.

Build the CLI and Rust SDK with the command in README.md. Run the project verification skills after relevant changes. `scripts/verify-formal.py --mutation` changes capture_registry.rs temporarily; do not edit that file concurrently. Plain tests must not inject host input or access user clipboard state. Native desktop interaction tests require isolated fixtures and explicit opt-in.

Use AGENTS.md for project instructions. Do not create CLAUDE.md. Do not publish or push based solely on a successful local check.
