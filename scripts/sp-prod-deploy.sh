#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/sp-prod-deploy.sh [options]

Safe, repeatable production deploy helper for the Smarty Pants Zulip droplet.

This script exists to prevent a specific production footgun:

- In production, Zulip uses a staticfiles manifest at DEPLOY_ROOT/staticfiles.json.
- Running collectstatic without ZULIP_COLLECTING_STATIC=1 can generate an empty manifest.
- Result: the site 500s with "Missing staticfiles manifest entry for ...".

Options:
  --deploy-root <path>     Defaults to the repo root containing this script.
  --host <hostname>        Defaults to smarty-pants.smartypants.ai (used for local curl Host header).
  --no-pull                Skip git pull.
  --no-webpack             Skip ./tools/webpack.
  --no-collectstatic        Skip manage.py collectstatic.
  --no-restart             Skip scripts/restart-server.
  --check-only             Only validate manifest + local HTTP endpoints.
  -h, --help               Show help.

Typical usage (on droplet as root):
  /opt/smarty-ops/zulip-deploy
EOF
}

log() {
  printf '[%s] sp-prod-deploy %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

DEPLOY_ROOT=""
HOST="smarty-pants.smartypants.ai"
DO_PULL=1
DO_WEBPACK=1
DO_COLLECTSTATIC=1
DO_RESTART=1
DO_CHECK=1

while [ $# -gt 0 ]; do
  case "$1" in
    --deploy-root)
      DEPLOY_ROOT="${2:-}"; shift 2 ;;
    --host)
      HOST="${2:-}"; shift 2 ;;
    --no-pull)
      DO_PULL=0; shift ;;
    --no-webpack)
      DO_WEBPACK=0; shift ;;
    --no-collectstatic)
      DO_COLLECTSTATIC=0; shift ;;
    --no-restart)
      DO_RESTART=0; shift ;;
    --check-only)
      DO_PULL=0; DO_WEBPACK=0; DO_COLLECTSTATIC=0; DO_RESTART=0; DO_CHECK=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      die "unknown arg: $1" ;;
  esac
done

if [ -z "$DEPLOY_ROOT" ]; then
  # Resolve to repo root: scripts/.. (works even when called via wrapper).
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  DEPLOY_ROOT="$(cd "$script_dir/.." && pwd)"
fi

[ -d "$DEPLOY_ROOT" ] || die "deploy root not found: $DEPLOY_ROOT"

log "deploy_root=$DEPLOY_ROOT"

if [ "$DO_PULL" -eq 1 ]; then
  log "+ git pull --ff-only"
  sudo -u zulip git -C "$DEPLOY_ROOT" pull --ff-only
fi

if [ "$DO_WEBPACK" -eq 1 ]; then
  log "+ tools/webpack --quiet"
  sudo -u zulip "$DEPLOY_ROOT/tools/webpack" --quiet
fi

if [ "$DO_COLLECTSTATIC" -eq 1 ]; then
  # CRITICAL: ZULIP_COLLECTING_STATIC=1 ensures STATICFILES_DIRS includes DEPLOY_ROOT/static.
  log "+ manage.py collectstatic (ZULIP_COLLECTING_STATIC=1)"
  sudo -u zulip env ZULIP_COLLECTING_STATIC=1 "$DEPLOY_ROOT/manage.py" collectstatic --noinput --clear
fi

if [ "$DO_CHECK" -eq 1 ]; then
  log "+ validate staticfiles.json and key bundles"
  DEPLOY_ROOT="$DEPLOY_ROOT" python3 - <<'PY'
import json
import os
import re
import sys

deploy_root = os.environ.get("DEPLOY_ROOT", "")
if not deploy_root:
    print("missing DEPLOY_ROOT env")
    sys.exit(2)
manifest_path = os.path.join(deploy_root, "staticfiles.json")

if not os.path.exists(manifest_path):
    print(f"missing manifest: {manifest_path}")
    sys.exit(2)

with open(manifest_path, "r", encoding="utf-8") as f:
    j = json.load(f)

paths = j.get("paths") or {}
if not isinstance(paths, dict):
    print("invalid manifest: paths is not a dict")
    sys.exit(2)

if len(paths) < 1000:
    print(f"manifest suspiciously small: paths_count={len(paths)}")
    sys.exit(2)

stats_path = os.path.join(deploy_root, "webpack-stats-production.json")
if not os.path.exists(stats_path):
    print(f"missing webpack stats: {stats_path}")
    sys.exit(2)

with open(stats_path, "r", encoding="utf-8") as f:
    stats = json.load(f)

chunks = stats.get("chunks") or {}
if not isinstance(chunks, dict):
    print("invalid webpack stats: chunks is not a dict")
    sys.exit(2)

entry = chunks.get("app")
if not isinstance(entry, list) or not entry:
    print("invalid webpack stats: missing chunks['app']")
    sys.exit(2)

static_root = "/home/zulip/prod-static"
missing = []
for filename in entry:
    if not isinstance(filename, str) or not filename:
        continue
    key = "webpack-bundles/" + filename
    if key not in paths:
        missing.append(key)
        continue
    rel = paths.get(key) or key
    p = os.path.join(static_root, rel)
    if not os.path.exists(p):
        missing.append(key + " (file missing)")

if missing:
    print("manifest missing entries required by webpack stats (app entrypoint):")
    for k in missing[:25]:
        print("-", k)
    if len(missing) > 25:
        print(f"... and {len(missing) - 25} more")
    sys.exit(2)

print(f"ok: manifest paths_count={len(paths)}")
print(f"ok: webpack app chunks={len(entry)}")
PY

  log "+ local HTTP smoke"
  root_code="$(curl -sS -o /dev/null -w '%{http_code}' -H "Host: $HOST" http://127.0.0.1/ || true)"
  login_code="$(curl -sS -o /dev/null -w '%{http_code}' -H "Host: $HOST" http://127.0.0.1/login/ || true)"
  if [ "$root_code" != "302" ]; then
    die "expected GET / -> 302, got $root_code"
  fi
  if [ "$login_code" != "200" ]; then
    die "expected GET /login/ -> 200, got $login_code"
  fi
  log "ok: GET / -> $root_code; GET /login/ -> $login_code"
fi

if [ "$DO_RESTART" -eq 1 ]; then
  log "+ scripts/restart-server"
  sudo -u zulip "$DEPLOY_ROOT/scripts/restart-server"
  log "ok: restart-server"
fi

log "done"