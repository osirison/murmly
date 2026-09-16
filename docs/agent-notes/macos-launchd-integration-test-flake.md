---
title: A red macOS job on CI is usually the launchd integration test, not your branch
description: test_install_status_stop_start_remove_against_real_launchctl fails intermittently on the GitHub macOS runners; check whether main fails the same way before investigating the diff
trigger: gh pr checks, unittest (macOS, Python 3.12), LaunchdServiceRuntimeIntegrationTests, launchctl reports, gh run rerun

depends_on: .github/workflows/tests.yml, tests/test_installer.py
recorded: 2026-09-16
---

# A red macOS job is usually the launchd test

**Symptom:** one macOS job in the Tests workflow fails while the other five
unittest jobs pass, with

```
murmly.installer.InstallError: launchctl reports gui/501/net.local.murmly-test-<pid>
is not running after bootstrap; run 'launchctl print gui/501/...' to see why.
ERROR: test_install_status_stop_start_remove_against_real_launchctl
```

The test bootstraps a real launchd service on the runner and polls for it to come
up. On a loaded GitHub macOS runner it sometimes does not come up inside the poll
window. Nothing about the branch under test is involved — it fails the same way on
`main`.

**Before investigating your own diff**, check whether `main` has failed the same
way:

```bash
gh run list --branch main --workflow Tests --limit 5 \
  --json databaseId,conclusion --jq '.[] | "\(.conclusion) \(.databaseId)"'
gh run view <id> --log-failed | grep -E "(ERROR|FAIL): test"
```

Seen failing on `main` on 2026-09-04 and again on an unrelated branch on
2026-09-16. If it is this test, re-run the job rather than changing anything:

```bash
gh run rerun <run-id> --failed
```

It has passed on the re-run both times.

**What would actually fix it** is the test waiting on launchd for longer, or
skipping itself when the runner refuses to bootstrap the service, rather than
turning a slow runner into a red build. Until then this note is what stops the
next session reading a clean branch for an hour.
