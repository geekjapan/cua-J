# Tool discovery

## Sub-features

List registered tools and inspect a tool input schema.

## How to get to it (user POV)

Run `cua-driver list-tools`, then `cua-driver describe get_screen_size`.

## Driving it with the CLI helper

Run `python3 scripts/verify-runtime.py`. Check tools.json contains get_screen_size and schema.json contains valid JSON.

## Gotchas

Catalog success does not prove the tool can execute.
