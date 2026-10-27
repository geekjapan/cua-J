# Display dimensions

## Sub-features

Read the logical main display dimensions and backing scale without injecting input.

## How to get to it (user POV)

Call `cua-driver call get_screen_size '{}' --socket <owned socket>` after launching the disposable daemon.

## Driving it with the CLI helper

Run `python3 scripts/verify-runtime.py`. screen.json records the action and result.json records positive width and height from the native desktop.

## Gotchas

A headless session fails this journey. This tool requires no TCC permission and cannot establish screenshot or accessibility permission.
