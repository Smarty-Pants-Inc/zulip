# Upstream Sync (Smarty Pants Zulip Fork)

This repo is a fork of https://github.com/zulip/zulip.

Goal: keep upstream merges routine and low-friction by maintaining a small, reviewable patch stack.

## Branch model

- `mirror/upstream-main`: mirror of upstream `main` (fast-forward only)
- `sp/main`: Smarty Pants patch stack on top of `mirror/upstream-main`

Guideline: land Smarty Pants changes into `sp/main` (keep the mirror branch fast-forwardable).

## Syncing upstream

From a clean working tree:

```bash
./tools/smarty-pants/sync-upstream.sh
```

This will:

- fetch upstream `main`
- fast-forward `mirror/upstream-main` to match upstream
- merge `mirror/upstream-main` into `sp/main`
- push both branches to `origin`

## Upstream PR workflow

When submitting a change upstream, we keep the same patch in `sp/main` until upstream resolves it.

- Open an upstream PR (against `zulip/zulip`).
- Immediately backport the patch into our fork (`sp/main`) via a small PR.
- Once upstream merges an acceptable solution, drop the local patch during the next sync (or explicitly revert it).
- If upstream rejects the change, decide whether to keep the patch locally or remove it.

## Keeping the patch stack clean

- Prefer small PRs into `sp/main`.
- Avoid invasive refactors unless they are isolated and easy to drop during future upstream merges.
- When syncing upstream, use `git range-diff` to sanity-check changes:

```bash
git fetch upstream main

git range-diff upstream/main...sp/main
```
