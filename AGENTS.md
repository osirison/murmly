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
uv run --extra cuda python -m unittest discover -s tests
```

The suite is stdlib `unittest` with no external test dependencies. Tests that need a
live desktop session skip themselves when it is unavailable rather than failing.
