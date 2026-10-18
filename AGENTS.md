# 多agent协作

This is the source distribution of the local Model Desk workbench.
Respect the optional globally installed Model Desk workflow switch; importing
modules or listing tools must never start services or dispatch model work.
Core guides are templates in `.local-worker/workflow-guides/` and must be
bound to the actual installation path before optional global-rule installation.
Keep project identity, per-project records, fee authorization and independent
acceptance intact. Exclude `.model-desk`, runtime homes and generated records
from source searches. Tests use isolated temporary records and mock providers.
Do not publish runtime configuration, secrets, logs, sessions or account data.

## Git workflow

Read `docs/GIT_WORKFLOW.md`. Develop on short-lived branches in isolated worktrees, inspect staged paths and diffs, and run meaningful offline checks before committing or proposing a merge. Preserve user modifications and the published portable installation. The local `baseline/*` recovery history must not be pushed or merged into public branches; migrate only reviewed changes onto branches based on `main`. Do not auto-stash, hard-reset, destructively clean or force-push. Worktree isolation does not isolate ports, accounts or databases; do not copy runtime keys/records or start real model work as a regression test. Git housekeeping does not authorize service deployment or public publishing.
