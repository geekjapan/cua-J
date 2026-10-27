# Verify native decision providers

Run the locked Python tests from `libs/cua-driver/examples/jev-use` with
`uv run --frozen python -m unittest discover -s python/tests`. They use fake
transports and Drivers. They must not send host input, read the clipboard,
or call paid inference APIs.

For live verification, obtain explicit authorization for API calls and native
input. Follow the [provider configuration and owned daemon setup](../../../../libs/cua-driver/examples/jev-use/decision-models.md#remote-decision-providers).
Use the built release binary, an explicit private socket, and a fresh output
directory. Do not attach an unrelated user daemon.

Run `benchmark_native.py --repetitions 1` with the owned `--driver-socket`
for a provider smoke. Require all three providers and all three tasks to have
independently verified fixture states. A successful model response or Driver
exit is insufficient. Distractor actions, uncertain delivery, and incomplete
trials must remain visible.

For comparison, use at least five rotated repetitions per provider and task.
Retain the source and binary hashes, complete trial records, error counts,
nullable token usage, phase timings, and independent oracle results. Profile
separately from the reported trials. State the limiter and the limits of the
three synthetic tasks before claiming a performance or accuracy difference.

The controller must reject unknown candidates, mismatched observations, expired
selections, invalid distributions, ties, and insufficient probability or margin
without dispatch. Refusal and abstention are distinct from successful input.
Unknown action delivery must be reconciled or stop before another input. A
SIGTERM check must prove cleanup of the exact owned fixture and runner groups.

This journey covers the Python native example. TypeScript, browser runners,
arbitrary applications, and model-level determinism require separate checks.
