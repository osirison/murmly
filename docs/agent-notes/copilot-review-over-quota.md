---
title: Copilot review over quota is not a review
description: Copilot posts a COMMENTED review saying it could not review; it does not satisfy the reviewer-comment rule
trigger: gh pr view --comments, gh api pulls reviews, Copilot, "reached their quota limit"
depends_on: repository Copilot code review setting
recorded: 2026-10-12
---

# A quota message from Copilot is not a review

**Symptom:** the PR gets a review by Copilot, state `COMMENTED`, reading "Copilot
was unable to review this pull request because the user who requested the review
has reached their quota limit."

**What to do:** treat the PR as having no review. Do not count it toward the
reviewer-comment condition for merging. Ask the maintainer how to proceed.
