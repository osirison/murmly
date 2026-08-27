---
title: Set the Pages source to GitHub Actions by hand before the first deploy
description: actions/deploy-pages fails until Settings - Pages - Source is switched from "Deploy from a branch" to "GitHub Actions", and no workflow can set it for itself
trigger: pages.yml, actions/deploy-pages, actions/configure-pages, actions/upload-pages-artifact, gh-pages, GitHub Pages deploy

depends_on: .github/workflows/pages.yml, site/
recorded: 2026-08-27
---

# Set the Pages source to GitHub Actions before the first deploy

**Symptom:** `actions/configure-pages` or `actions/deploy-pages` fails on the
first run of a new Pages workflow, on a repository that has never published a
page. The workflow file is correct and the error does not name the setting.

**Fix:** a human with admin rights opens **Settings → Pages → Source** and picks
**GitHub Actions** instead of *Deploy from a branch*. There is no API call in the
workflow that can do this for itself, and no amount of re-running helps.

**Why it was not obvious:** every other part of publishing is in the repository,
so the one prerequisite that is not looks like a bug in the workflow. Treat it as
a step in the change's migration plan, before the merge, not as something to
debug afterwards.

**Verify by loading the page, not by the badge.** A green workflow means the
artifact uploaded and the deployment was accepted. It does not mean every asset
resolves: the project page is served from `/<repo>/`, so any reference written
from the host root 404s while the run still reports success. Open
`https://<owner>.github.io/<repo>/` and check the network log.
