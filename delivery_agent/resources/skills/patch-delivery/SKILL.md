---
name: patch-delivery
description: Finish repository changes as a reproducible patch with focused checks and evidence; use before submitting a coding task.
---

Inspect the final diff and remove debugging files or accidental formatting changes. Ensure new implementation and regression-test files are present in the worktree. Leave Git HEAD at the frozen task commit; do not commit inside the agent workspace.

Run the task's public checks and record their commands and return codes. If a check fails, report the failure and the smallest supporting evidence. Do not label a task solved because the submission marker was accepted. The harness exports actual file changes, applies the patch to a fresh checkout, and runs independent checks.

Search prior evidence with delivery-history when a previous failure constrains the fix. Search delivery-knowledge for relevant verified records when it is available, but compare their repository, base commit, expiry, and verification provenance before transferring advice. A note or retrieved past solution is not a substitute for this task's checks.
