# Working on murmly

## Field notes

Before running an unfamiliar build, deployment, or CLI command in this repo,
grep `docs/agent-notes/` for the command name. Those notes record preconditions
that are not documented anywhere else.

## Specs

Behavioral changes are planned with OpenSpec. `openspec/specs/` holds the current
capability baseline; `openspec/changes/` holds in-flight changes and their archive.
Run `openspec list` to see what is active.

## Tests

```bash
uv run --no-sync python -m unittest discover -s tests
```

The suite is stdlib `unittest` with no external test dependencies. Tests that need a
live desktop session skip themselves when it is unavailable rather than failing.

Pass `--no-sync` rather than naming an extra. `uv run --extra cuda` resyncs the
environment, which reinstalls the CPU build of `onnxruntime` over the GPU build on a
machine that has had the swap applied. The suite still passes, and every synthesis
measurement taken afterwards silently reports a CPU session. See
`docs/agent-notes/onnxruntime-gpu-cuda-version.md`.

# Commit Comments
NEVER USE `🤖 Generated with Claude Code`