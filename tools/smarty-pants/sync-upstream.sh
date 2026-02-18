#!/usr/bin/env bash
set -euo pipefail

# Sync this fork with upstream Zulip using a simple patch-stack model.
#
# Branch model:
# - origin/mirror/upstream-main : mirror of upstream/main (fast-forward only)
# - origin/sp/main              : Smarty Pants patch stack on top of the mirror
#
# This script updates both branches locally and pushes them to origin.

UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/zulip/zulip.git}"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"

ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"
MIRROR_BRANCH="${MIRROR_BRANCH:-mirror/upstream-main}"
PATCH_BRANCH="${PATCH_BRANCH:-sp/main}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: must run inside a git repo" >&2
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: working tree is dirty; commit/stash first" >&2
    exit 1
fi

# Ensure upstream remote exists.
if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
    git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
fi

git fetch "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"

# Update mirror branch (fast-forward only).
if git show-ref --verify --quiet "refs/heads/$MIRROR_BRANCH"; then
    git switch "$MIRROR_BRANCH"
else
    git switch -c "$MIRROR_BRANCH" "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
fi

git merge --ff-only "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
git push "$ORIGIN_REMOTE" "$MIRROR_BRANCH"

# Update patch branch by merging the mirror.
#
# Note: This keeps sp/main as a readable patch stack on top of main.
# If you prefer a rebase-based stack, do that manually and force-push.
if git show-ref --verify --quiet "refs/heads/$PATCH_BRANCH"; then
    git switch "$PATCH_BRANCH"
else
    git switch -c "$PATCH_BRANCH" "$ORIGIN_REMOTE/$PATCH_BRANCH"
fi

git merge --no-edit "$MIRROR_BRANCH"
git push "$ORIGIN_REMOTE" "$PATCH_BRANCH"

echo "OK: synced $MIRROR_BRANCH and $PATCH_BRANCH"
