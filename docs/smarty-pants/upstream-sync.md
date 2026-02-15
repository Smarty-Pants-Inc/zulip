# Upstream Sync (Smarty Pants Zulip Fork)

This repo is a fork of https://github.com/zulip/zulip.

Goal: keep upstream merges routine and low-friction by maintaining a small, reviewable patch stack.

## Branch model

- `main`: mirror of upstream `main` (fast-forward only)
- `sp/main`: Smarty Pants patch stack on top of `main`

Guideline: land Smarty Pants changes into `sp/main` (not `main`).

## Syncing upstream

From a clean working tree:

```bash
./tools/smarty-pants/sync-upstream.sh
```

This will:

- fetch upstream `main`
- fast-forward `main` to match upstream
- merge `main` into `sp/main`
- push both branches to `origin`

## Keeping the patch stack clean

- Prefer small PRs into `sp/main`.
- Avoid invasive refactors unless they are isolated and easy to drop during future upstream merges.
- When syncing upstream, use `git range-diff` to sanity-check changes:

```bash
git fetch upstream main

git range-diff upstream/main...sp/main
```
