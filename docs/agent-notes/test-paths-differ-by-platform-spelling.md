---
title: Test paths differ by platform spelling; compare content, not strings
description: Windows temp dirs switch between 8.3 short names and long names; macOS temp is a symlink; refusing dangling symlinks breaks on symlinked /home
trigger: tests comparing paths, os.path.realpath, symlink tests, settings_writer tests, path strings in messages
depends_on: tests/test_settings_writer.py, tests/test_removal.py, .github/workflows/tests.yml, "#98"
recorded: 2026-10-13
---

## Windows 8.3 short names

Windows CI temp dirs return different names from different calls. `realpath` of an
existing path expands `C:\Users\RUNNER~1\...` to the long form `C:\Users\runneradmin\...`,
but `realpath` of a dangling link keeps the name the link says. This broke tests
that compared full path strings or used `in <path>` checks.

## macOS temp is under /var, a symlink to /private/var

Any check like "refuse if a symlink is on the path" fires on every macOS path.
The same rule breaks on Fedora Silverblue, where `/home` → `var/home`. Only refuse
DANGLING symlinks. To simulate macOS locally, run with TMPDIR pointing through
a symlink.

## What to do

Compare distinctive path components or file contents, not full path strings:

```python
self.assertIn(Path("subdir") / "file.txt", resolved_path)
```

or

```python
with open(actual_path) as f:
    self.assertEqual(f.read(), expected_contents)
```

Refuse only dangling symlinks in the resolved path, not every symlink:

```python
assert not any(part.is_symlink() and not part.exists() for part in resolved_path.parents)
```

This pattern works across Windows, macOS, and Linux, including Silverblue.
