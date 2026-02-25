# Zulip Fork Workflow (Smarty Pants)

This repository is a fork of the upstream Zulip server:

- Upstream: https://github.com/zulip/zulip
- Fork: https://github.com/Smarty-Pants-Inc/zulip

Goal: keep upstream merges routine and low-friction by maintaining a small, reviewable patch stack and a clear integration process.

## Branch Model

We use two long-lived branches:

- `mirror/upstream-main`
  - A *pure mirror* of upstream `main`.
  - Policy: fast-forward only.

- `sp/main`
  - The Smarty Pants patch stack on top of `mirror/upstream-main`.
  - Policy: contains all fork-specific changes required for our deployment.

Optional safety markers:

- `sp/archive/<timestamp>`
  - A named pointer to an old `sp/main` tip before a risky operation (e.g. a large upstream sync).
- `sp/base/<yyyy-mm-dd>`
  - A named pointer to the upstream base commit for a given sync cycle.

## File/Folder Conventions

These paths are fork-specific and intended to make maintenance predictable:

- `docs/smarty-pants/`
  - Fork maintenance docs (this file, upstream sync notes, etc.).

- `tools/smarty-pants/`
  - Helper scripts for fork maintenance.

## Upstream Sync Procedure

We generally prefer *merge-based* upstream syncing for safety and traceability.

From a clean working tree:

```bash
./tools/smarty-pants/sync-upstream.sh
```

What it does:

1) Fetch upstream `main`.
2) Fast-forward `mirror/upstream-main`.
3) Merge `mirror/upstream-main` into `sp/main`.
4) Push both branches to `origin`.

If the merge produces conflicts, resolve them, run tests, then push.

## Working On Fork Changes (Grouped Commit Sets)

We keep fork changes reviewable by grouping related commits together and keeping each group small.

Recommended practice:

- Use topic branches for work-in-progress, e.g.
  - `sp/topic/sp_ai-widget`
  - `sp/topic/facade`
  - `sp/topic/replay-hardening`

- When a topic is ready, land it into `sp/main` via a small PR.

- Keep commits scoped and descriptive; treat commit messages as the *primary index* for understanding the patch stack.

Even when commits are grouped via topic branches, the durable integration artifact remains `sp/main`.

## Sanity Checks During Syncs

Useful commands:

```bash
git fetch upstream main
git fetch origin

# What is in our fork but not in upstream?
git range-diff upstream/main...sp/main

# Quick glance at fork-only commits
git log --oneline upstream/main..sp/main
```

## Updating Downstream Consumers

This fork is consumed by downstream repos (e.g. via git submodule pointers).

After updating `sp/main`:

1) Update the consumer to point at the new `sp/main` commit.
2) Run the relevant E2E tests.
3) Deploy.

## Safety Rules

- Do not rewrite `sp/main` history unless you have a very strong reason.
- Before a risky operation, create and push an archive marker.
- Keep patches small and easy to drop when upstream eventually implements an equivalent feature.
