---
title: Test paths differ by platform spelling; compare content, not strings
description: Windows temp dirs switch between 8.3 short names and long names; macOS temp is a symlink; a rule that refuses every symlink breaks on symlinked /home
trigger: tests comparing paths, os.path.realpath, symlink tests, settings_writer tests, path strings in messages
depends_on: tests/test_settings_writer.py, src/murmly/settings_writer.py, .github/workflows/tests.yml, "#98"
recorded: 2026-10-13
---

## Windows 8.3 short names

Windows CI temp dirs return different names from different calls. `realpath` of an
existing path expands `C:\Users\RUNNER~1\...` to the long form `C:\Users\runneradmin\...`,
but `realpath` of a dangling link keeps the name the link says. This broke tests
that compared full path strings.

## macOS temp is under /var, a symlink to /private/var

Fedora Silverblue has the same shape: `/home` is a link to `var/home`. Any check
that refuses a path because a link is on it fires on every such machine. To simulate
macOS locally, run the tests with TMPDIR pointing through a symlink.

## What to do

In tests, compare distinctive path components or file contents, not full path
strings.

For writer behaviour, see `src/murmly/settings_writer.py`: it refuses only a dangling
link or junction on the path, and only when the target's folder is missing (#98).
