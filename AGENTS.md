# Working on murmly

## Field notes

Before running an unfamiliar build, deployment, or CLI command in this repo,
grep `docs/agent-notes/` for the command name. Those notes record preconditions
that are not documented anywhere else.

## Specs

Behavioral changes are planned with OpenSpec. `openspec/specs/` holds the current
capability baseline; `openspec/changes/` holds in-flight changes and their archive.
Run `openspec list` to see what is active.

A requirement's text, the prose between its heading and its first scenario, must be
500 characters or fewer: CI's `openspec validate --all --strict` fails on anything
longer, in a baseline spec or in a change's ADDED requirement. When a requirement
grows past that, split it into requirements that each state one behaviour, or move
its examples and edge cases into scenarios.

## Tests

```bash
uv run --no-sync python -m unittest discover -s tests
```

The suite is stdlib `unittest` with no external test dependencies. Tests that need a
live desktop session skip themselves when it is unavailable rather than failing.

`--no-sync` is what makes this safe, and it is needed whether or not an extra is
named. `uv run` syncs before it runs anything, and the CPU build of `onnxruntime`
arrives as a dependency of `faster-whisper`, so any sync reinstalls it over the GPU
build on a machine that has had the swap applied. The suite still passes, and every
synthesis measurement taken afterwards silently reports a CPU session. This is the
command CI runs. See `docs/agent-notes/onnxruntime-gpu-cuda-version.md`.

## Dependencies

Dependency updates arrive as Dependabot pull requests a few days after a release
(`.github/dependabot.yml`), and `.github/workflows/dependabot-auto-merge.yml` turns on
auto-merge for them, so they merge themselves once the required checks pass. Two kinds
are left for a person to merge: GitHub Actions updates, because they edit workflow
files, and the `nvidia-*` packages, because CI never installs the `cuda` extra.
Security updates skip the few days' wait and the major-version ignores, and are
auto-merged the same way.

A merge made by the workflow starts no workflow runs on `main`: Tests does not run on
the new commit (the run on the pull request is the check), and the manual is rebuilt by
the weekly schedule in `pages.yml`, not by the merge.

The required checks are listed by exact name in a repository ruleset on the default
branch (Settings, Rules), not in the workflow files. Renaming a job in `tests.yml`, or
changing a matrix label such as the Python version or the operating system, leaves the
old name waiting for a result that never comes, and every pull request stays blocked,
yours included, until the ruleset is edited to match, or you use the admin bypass on
the pull request. A job added to `tests.yml` gates nothing until it is added to the
ruleset too.

The OpenSpec CLI pin in `tests.yml` is an `npm install` inside a step, which Dependabot
cannot see. Move it by hand, together with any spec rewrites the new version needs.

# Commit Comments
NEVER USE `🤖 Generated with Claude Code`
